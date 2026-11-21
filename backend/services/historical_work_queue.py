"""Bounded durable historical batches, claimed by PostgreSQL session locks."""
from contextlib import contextmanager
import time

from services import persistent_store as store

STATE_KEY = "news_shared_queue:v1"
LOCK_NAMESPACE = 18430627
SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_llm_work (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    published_ts BIGINT NOT NULL,
    first_url TEXT NOT NULL,
    articles JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS historical_llm_work_order
    ON historical_llm_work (published_ts DESC, first_url DESC);
"""


def connect():
    return store.psycopg.connect(store.DATABASE_URL, row_factory=store.dict_row,
                                autocommit=True)


def ensure_schema():
    with connect() as conn, conn.transaction():
        # IF NOT EXISTS alone does not serialize concurrent PostgreSQL catalog writes.
        conn.execute("SELECT pg_advisory_xact_lock(%s, 0)", (LOCK_NAMESPACE,))
        conn.execute(SCHEMA)


def _load(conn, name=STATE_KEY, lock=False):
    row = conn.execute("SELECT value FROM worker_checkpoints WHERE name = %s"
                       + (" FOR UPDATE" if lock else ""), (name,)).fetchone()
    return dict(row["value"]) if row else {}


def _save(conn, state):
    conn.execute("""INSERT INTO worker_checkpoints (name, value) VALUES (%s, %s)
        ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                 (STATE_KEY, store.jsonb(state)))


def initialize(primary_name, helper_name, newest, count_remaining):
    with connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, 0)", (LOCK_NAMESPACE,))
        existing = _load(conn)
        if existing:
            if set(existing["workers"]) != {primary_name, helper_name}:
                raise ValueError("Shared queue pipeline identity changed")
            if "tracking_started_at" not in existing:
                existing["tracking_started_at"] = int(time.time())
                existing["tracking_baseline"] = sum(w["completed"] for w in existing["workers"].values())
                _save(conn, existing)
            return
        primary, helper = _load(conn, primary_name), _load(conn, helper_name)
        if (not primary.get("newest_first_v2")
                or primary.get("phase") not in {"historical_articles", "complete"}):
            raise ValueError("Finish legacy calendar initialization before shared queue migration")
        before = int(primary.get("historical_before_published_ts") or newest() + 1)
        url = str(primary.get("historical_before_url") or "")
        remaining = count_remaining(before_published_ts=before, before_url=url)
        workers = {name: {"completed": int(old.get("historical_completed") or 0),
                          "calendar_completed": int(old.get("calendar_completed") or 0)}
                   for name, old in ((primary_name, primary), (helper_name, helper))}
        _save(conn, {"before_published_ts": before, "before_url": url,
                     "remaining_to_scan": remaining, "source_exhausted": False,
                     "total": remaining + sum(w["completed"] for w in workers.values()),
                     "workers": workers, "legacy_checkpoints": {primary_name: primary, helper_name: helper},
                     "tracking_started_at": int(time.time()),
                     "tracking_baseline": sum(w["completed"] for w in workers.values()),
                     "skipped_existing": 0})


def fill(fetch, batch_size=8):
    if not 1 <= batch_size <= 8:
        raise ValueError("Invalid historical batch size")
    with connect() as conn, conn.transaction():
        # Only one cursor producer; cursor movement and queued payloads commit together.
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s, 0) AS ok",
                            (LOCK_NAMESPACE,)).fetchone()["ok"]:
            return
        state = _load(conn, lock=True)
        if not state or state["source_exhausted"]:
            return
        if conn.execute("SELECT count(*) AS n FROM historical_llm_work").fetchone()["n"] >= 2:
            return
        rows = fetch(before_published_ts=state["before_published_ts"],
                     before_url=state["before_url"], limit=100, min_published_ts=0)
        if not rows:
            state.update(source_exhausted=True, remaining_to_scan=0)
        else:
            urls = [row["url"] for row in rows]
            saved = {row["url"] for row in conn.execute(
                """SELECT url FROM historical_article_enrichments
                   WHERE url = ANY(%s) AND NOT (result ? 'processing_error')""", (urls,))}
            # Existing retry ownership stays with the bounded retry pass, not forward inference.
            retries = {row["url"] for row in conn.execute(
                "SELECT url FROM historical_article_retries WHERE url = ANY(%s)", (urls,))}
            pending = list({row["url"]: row for row in rows
                            if row["url"] not in saved | retries}.values())
            for start in range(0, len(pending), batch_size):
                batch = pending[start:start + batch_size]
                conn.execute("""INSERT INTO historical_llm_work (published_ts, first_url, articles)
                                VALUES (%s, %s, %s)""",
                             (int(batch[0].get("published_ts") or 0), batch[0]["url"], store.jsonb(batch)))
            state.update(before_published_ts=int(rows[-1].get("published_ts") or 0),
                         before_url=rows[-1]["url"],
                         remaining_to_scan=max(0, state["remaining_to_scan"] - len(rows)),
                         skipped_existing=state["skipped_existing"] + len(saved))
        _save(conn, state)
        return len(rows)


@contextmanager
def claim():
    # No long transaction or expiring lease during inference. A dead session releases
    # its lock; its durable batch remains for recovery. Completion is still at-least-once.
    with connect() as conn:
        job = None
        for candidate in conn.execute("""SELECT id FROM historical_llm_work
                ORDER BY published_ts DESC, first_url DESC LIMIT 32""").fetchall():
            identity = candidate["id"]
            if not conn.execute("SELECT pg_try_advisory_lock(%s, %s) AS ok",
                                (LOCK_NAMESPACE, identity)).fetchone()["ok"]:
                continue
            job = conn.execute("SELECT * FROM historical_llm_work WHERE id = %s",
                               (identity,)).fetchone()
            if job:
                break
            conn.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, identity))
        yield (conn, job) if job else None
        # Connection close releases only this session's claims, including on exceptions.


def finish(conn, job, worker, stats):
    with conn.transaction():
        owned = conn.execute("""SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
            AND pid = pg_backend_pid() AND classid = %s AND objid = %s
            AND objsubid = 2 AND granted""", (LOCK_NAMESPACE, job["id"])).fetchone()
        if not owned:
            raise RuntimeError("Historical batch claim is not owned by this session")
        # A batch can only be removed when every item has a durable outcome or retry.
        urls = [row["url"] for row in job["articles"]]
        accounted = {row["url"] for row in conn.execute("""
            SELECT url FROM historical_article_enrichments
            WHERE url = ANY(%s) AND NOT (result ? 'processing_error')
            UNION SELECT url FROM historical_article_retries WHERE url = ANY(%s)
            """, (urls, urls))}
        if set(urls) != accounted:
            raise RuntimeError("Historical batch has unsaved outcomes")
        if conn.execute("DELETE FROM historical_llm_work WHERE id = %s RETURNING id",
                        (job["id"],)).fetchone():
            _record(conn, worker, stats)


def _record(conn, worker, stats):
    state = _load(conn, lock=True)
    progress = state["workers"].setdefault(worker, {"completed": 0, "calendar_completed": 0})
    progress["completed"] += int(stats.get("completed_delta", 0))
    _save(conn, state)


def record_retry(worker, stats):
    with connect() as conn, conn.transaction():
        _record(conn, worker, stats)


def snapshot(worker):
    with connect() as conn:
        state = _load(conn)
        pending = conn.execute("""SELECT COALESCE(sum(jsonb_array_length(articles)), 0) AS n
                                   FROM historical_llm_work""").fetchone()["n"]
        retries = conn.execute("SELECT count(*) AS n FROM historical_article_retries").fetchone()["n"]
    progress = state.get("workers", {}).get(worker, {})
    return {"queue_mode": "shared_newest_first", "historical_completed": progress.get("completed", 0),
            "calendar_completed": progress.get("calendar_completed", 0),
            "historical_total": state.get("total", 0),
            "historical_remaining": state.get("remaining_to_scan", 0) + pending + retries,
            "shared_completed": sum(w["completed"] for w in state.get("workers", {}).values()),
            "shared_tracking_started_at": state.get("tracking_started_at"),
            "shared_tracking_baseline": state.get("tracking_baseline"),
            "historical_cursor_published_ts": state.get("before_published_ts", 0),
            "shared_queue_pending": pending, "source_exhausted": state.get("source_exhausted", False),
            "historical_eta_seconds": None}
