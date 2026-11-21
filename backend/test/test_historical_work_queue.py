"""Real concurrent sessions, with all writes confined to a disposable schema."""
import os
import uuid
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from services import historical_work_queue as queue


@pytest.fixture
def database(monkeypatch):
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "true":
        pytest.skip("RUN_POSTGRES_INTEGRATION=true enables isolated queue integration tests")
    from psycopg import connect, sql
    from psycopg.rows import dict_row
    schema = "news_queue_test_" + uuid.uuid4().hex
    monkeypatch.setattr(queue, "LOCK_NAMESPACE", 20000000 + int(uuid.uuid4().hex[:6], 16))
    with connect(queue.store.DATABASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.setattr(queue, "connect", lambda: connect(
            queue.store.DATABASE_URL, autocommit=True, row_factory=dict_row,
            options=f"-c search_path={schema}"))
        try:
            with queue.connect() as conn:
                conn.execute("""CREATE TABLE worker_checkpoints
                    (name TEXT PRIMARY KEY, value JSONB, updated_at TIMESTAMPTZ DEFAULT NOW());
                    CREATE TABLE historical_article_enrichments (url TEXT PRIMARY KEY, result JSONB);
                    CREATE TABLE historical_article_retries (url TEXT PRIMARY KEY);""")
                conn.execute("INSERT INTO worker_checkpoints (name,value) VALUES (%s,%s)",
                             ("primary", queue.store.jsonb({"newest_first_v2": True,
                              "phase": "historical_articles", "historical_before_published_ts": 100,
                              "historical_before_url": "z", "historical_completed": 10})))
                conn.execute("INSERT INTO worker_checkpoints (name,value) VALUES (%s,%s)",
                             ("helper", queue.store.jsonb({"historical_completed": 20})))
            queue.ensure_schema()
            queue.initialize("primary", "helper", lambda: 200, lambda **kw: 1000)
            yield
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def articles(n=16):
    return [{"url": f"https://test.invalid/{i:03d}", "published_ts": 99 - i}
            for i in range(n)]


def save_results(conn, rows):
    for row in rows:
        conn.execute("INSERT INTO historical_article_enrichments VALUES (%s, '{}'::jsonb)",
                     (row["url"],))


def test_simultaneous_first_schema_creation(database):
    with queue.connect() as conn:
        conn.execute("DROP TABLE historical_llm_work")
    barrier = Barrier(2)

    def start():
        barrier.wait(timeout=10)
        queue.ensure_schema()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)
    with queue.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM historical_llm_work").fetchone()["n"] == 0


def test_migration_keeps_legacy_checkpoints_and_is_idempotent(database):
    with queue.connect() as conn:
        state = queue._load(conn)
        assert state["before_published_ts"] == 100
        assert state["before_url"] == "z"
        assert queue._load(conn, "primary") == state["legacy_checkpoints"]["primary"]
    queue.initialize("primary", "helper", lambda: pytest.fail("must not reset"), lambda **kw: 0)
    assert queue.snapshot("primary")["historical_completed"] == 10
    assert queue.snapshot("helper")["historical_completed"] == 20
    with pytest.raises(ValueError, match="identity"):
        queue.initialize("different", "helper", lambda: 1, lambda **kw: 0)


def test_tracking_migration_is_durable_without_resetting_completed_work(database, monkeypatch):
    with queue.connect() as conn:
        state = queue._load(conn)
        state.pop("tracking_started_at")
        state.pop("tracking_baseline")
        queue._save(conn, state)
    monkeypatch.setattr(queue.time, "time", lambda: 1000)
    queue.initialize("primary", "helper", lambda: pytest.fail("reset"), lambda **kw: 0)
    queue.record_retry("primary", {"completed_delta": 5})
    monkeypatch.setattr(queue.time, "time", lambda: 2000)
    queue.initialize("primary", "helper", lambda: pytest.fail("reset"), lambda **kw: 0)
    snapshot = queue.snapshot("helper")
    assert snapshot["shared_tracking_started_at"] == 1000
    assert snapshot["shared_tracking_baseline"] == 30
    assert snapshot["shared_completed"] == 35


def test_two_workers_claim_distinct_newest_batches_and_ack_once(database):
    queue.fill(lambda **kw: articles())
    with ExitStack() as stack:
        first = stack.enter_context(queue.claim())
        second = stack.enter_context(queue.claim())
        assert first[1]["articles"] == articles()[:8]
        assert second[1]["articles"] == articles()[8:]
        assert stack.enter_context(queue.claim()) is None
        save_results(first[0], first[1]["articles"])
        queue.finish(*first, "primary", {"completed_delta": 8})
        queue.finish(*first, "primary", {"completed_delta": 8})
    assert queue.snapshot("primary")["historical_completed"] == 18
    assert queue.snapshot("helper")["historical_completed"] == 20
    with queue.claim() as recovered:
        assert recovered[1]["id"] == second[1]["id"]


def test_dead_session_releases_claim_and_stale_connection_cannot_ack(database):
    queue.fill(lambda **kw: articles(8))
    with queue.claim() as first:
        first[0].close()
        with queue.claim() as recovered:
            assert recovered[1]["id"] == first[1]["id"]
            with pytest.raises(queue.store.psycopg.Error):
                queue.finish(*first, "primary", {"completed_delta": 8})
            with queue.connect() as stranger, pytest.raises(RuntimeError, match="not owned"):
                queue.finish(stranger, recovered[1], "helper", {"completed_delta": 8})


def test_unsaved_batch_is_not_lost_and_durable_retry_counts_as_accounted(database):
    queue.fill(lambda **kw: articles(2))
    with queue.claim() as job:
        with pytest.raises(RuntimeError, match="unsaved"):
            queue.finish(*job, "primary", {"completed_delta": 2})
        save_results(job[0], job[1]["articles"][:1])
        job[0].execute("INSERT INTO historical_article_retries VALUES (%s)",
                       (job[1]["articles"][1]["url"],))
        queue.finish(*job, "primary", {"completed_delta": 1})
    assert queue.snapshot("primary")["historical_completed"] == 11
    assert queue.snapshot("primary")["shared_queue_pending"] == 0


def test_cursor_and_enqueue_rollback_together(database, monkeypatch):
    original = queue._save
    monkeypatch.setattr(queue, "_save", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        queue.fill(lambda **kw: articles())
    monkeypatch.setattr(queue, "_save", original)
    assert queue.snapshot("primary")["shared_queue_pending"] == 0
    with queue.connect() as conn:
        assert queue._load(conn)["before_published_ts"] == 100


def test_skip_saved_and_backoff_retries_without_inference_and_bound_prefetch(database):
    rows = articles(100)
    with queue.connect() as conn:
        save_results(conn, rows[:5])
        conn.execute("INSERT INTO historical_article_retries VALUES (%s)", (rows[5]["url"],))
    queue.fill(lambda **kw: rows)
    assert queue.snapshot("primary")["shared_queue_pending"] == 94
    queue.fill(lambda **kw: pytest.fail("bounded queue must not fetch again"))
    with queue.claim() as job:
        assert job[1]["articles"][0] == rows[6]


def test_partial_commit_survives_worker_loss_and_remains_available(database):
    queue.fill(lambda **kw: articles(8))
    with queue.claim() as first:
        save_results(first[0], first[1]["articles"][:4])
    with queue.claim() as recovered:
        saved = recovered[0].execute("SELECT count(*) AS n FROM historical_article_enrichments").fetchone()
        assert saved["n"] == 4
        assert recovered[1]["articles"] == articles(8)
        save_results(recovered[0], recovered[1]["articles"][4:])
        queue.finish(*recovered, "helper", {"completed_delta": 8})
    assert queue.snapshot("helper")["historical_completed"] == 28
