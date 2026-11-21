import hashlib
import json
import math
import os
import time
import threading
from contextlib import nullcontext
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None


DATABASE_URL = os.getenv("DATABASE_URL")
_SCHEMA_READY_FOR = None
_SCHEMA_LOCK = threading.Lock()
NEWS_LLM_VALIDATION_CATEGORIES = (
    "ticker",
    "relationship",
    "translated",
    "headline_changed",
    "unrelated",
    "unverified",
)
NEWS_LLM_REVIEW_TARGET_PER_CATEGORY = max(
    10,
    int(os.getenv("NEWS_LLM_REVIEW_TARGET_PER_CATEGORY", "30")),
)


def _wilson_interval(correct: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    proportion = correct / total
    denominator = 1 + (z * z / total)
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) / denominator
    return round(max(0.0, center - margin) * 100, 1), round(min(1.0, center + margin) * 100, 1)

_GRAPH_OUTBOX_SCHEMA = """CREATE TABLE IF NOT EXISTS historical_article_graph_outbox (
    url TEXT PRIMARY KEY,
    result JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_historical_article_graph_outbox_due
    ON historical_article_graph_outbox (next_attempt_at, url);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_history (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts BIGINT NOT NULL,
    source TEXT NOT NULL,
    watchlist TEXT,
    action TEXT,
    confidence DOUBLE PRECISION,
    score DOUBLE PRECISION,
    catalyst JSONB,
    sentiment JSONB,
    technicals JSONB,
    intraday JSONB,
    trade_plan JSONB,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_decision_history_symbol_ts
    ON decision_history (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_decision_history_ts
    ON decision_history (ts DESC);
CREATE TABLE IF NOT EXISTS scanner_settings (
    id TEXT PRIMARY KEY DEFAULT 'default',
    settings JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS scanner_results (
    symbol TEXT PRIMARY KEY,
    bucket TEXT NOT NULL,
    score DOUBLE PRECISION,
    rvol DOUBLE PRECISION,
    ts BIGINT,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scanner_results_bucket_score
    ON scanner_results (bucket, score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_scanner_results_ts
    ON scanner_results (ts DESC NULLS LAST);
CREATE TABLE IF NOT EXISTS calendar_states (
    session_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,
    event_date DATE NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT,
    symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION,
    extraction_source TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events (event_date DESC);
CREATE TABLE IF NOT EXISTS earnings_events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    company_name TEXT,
    event_date DATE NOT NULL,
    event_name TEXT,
    call_time TEXT,
    fiscal_quarter INTEGER,
    fiscal_year INTEGER,
    eps_estimate DOUBLE PRECISION,
    eps_actual DOUBLE PRECISION,
    surprise_percent DOUBLE PRECISION,
    market_cap_text TEXT,
    market_cap_value BIGINT,
    source_url TEXT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_earnings_events_date
    ON earnings_events (event_date DESC, symbol);
CREATE INDEX IF NOT EXISTS idx_earnings_events_symbol_date
    ON earnings_events (symbol, event_date DESC);
CREATE TABLE IF NOT EXISTS feature_store (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts BIGINT NOT NULL,
    source TEXT NOT NULL,
    price DOUBLE PRECISION,
    action TEXT,
    reward DOUBLE PRECISION,
    semantic_signal JSONB NOT NULL,
    market_signal JSONB NOT NULL,
    stop_loss JSONB NOT NULL,
    committee JSONB,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feature_store_symbol_ts
    ON feature_store (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_feature_store_ts
    ON feature_store (ts DESC);
CREATE TABLE IF NOT EXISTS trade_positions (
    position_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'LONG',
    state TEXT NOT NULL,
    intended_quantity INTEGER NOT NULL,
    filled_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    remaining_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    average_entry_price DOUBLE PRECISION,
    hard_stop_price DOUBLE PRECISION NOT NULL,
    active_stop_price DOUBLE PRECISION NOT NULL,
    peak_price DOUBLE PRECISION,
    partial_taken BOOLEAN NOT NULL DEFAULT FALSE,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    strategy_version TEXT NOT NULL,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_positions_one_active_symbol
    ON trade_positions (account_id, symbol)
    WHERE state NOT IN ('closed', 'blocked');
CREATE INDEX IF NOT EXISTS idx_trade_positions_account_state
    ON trade_positions (account_id, state, updated_at DESC);
CREATE TABLE IF NOT EXISTS trade_order_intents (
    idempotency_key TEXT PRIMARY KEY,
    position_id TEXT REFERENCES trade_positions(position_id),
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    intent_role TEXT NOT NULL DEFAULT 'discretionary',
    replaces_idempotency_key TEXT REFERENCES trade_order_intents(idempotency_key),
    order_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    broker_order_id BIGINT,
    broker_location TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_order_intents_account_status
    ON trade_order_intents (account_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_order_intents_broker_order
    ON trade_order_intents (broker_order_id)
    WHERE broker_order_id IS NOT NULL;
ALTER TABLE trade_order_intents
    ADD COLUMN IF NOT EXISTS filled_quantity DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE trade_order_intents
    ADD COLUMN IF NOT EXISTS remaining_quantity DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE trade_order_intents
    ADD COLUMN IF NOT EXISTS average_fill_price DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE trade_order_intents
    ADD COLUMN IF NOT EXISTS last_broker_payload JSONB;
ALTER TABLE trade_order_intents
    ADD COLUMN IF NOT EXISTS intent_role TEXT NOT NULL DEFAULT 'discretionary';
ALTER TABLE trade_order_intents
    ADD COLUMN IF NOT EXISTS replaces_idempotency_key TEXT REFERENCES trade_order_intents(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_trade_order_intents_position_role
    ON trade_order_intents (position_id, intent_role, updated_at DESC)
    WHERE position_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS trade_fills (
    fill_key TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL REFERENCES trade_order_intents(idempotency_key),
    position_id TEXT REFERENCES trade_positions(position_id),
    broker_order_id BIGINT NOT NULL,
    leg_id BIGINT NOT NULL,
    symbol TEXT,
    instruction TEXT,
    quantity DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    executed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_fills_order
    ON trade_fills (broker_order_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_trade_fills_position
    ON trade_fills (position_id, executed_at)
    WHERE position_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS trade_position_events (
    event_key TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES trade_positions(position_id),
    event_type TEXT NOT NULL,
    broker_order_id BIGINT,
    event_ts BIGINT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_position_events_position_ts
    ON trade_position_events (position_id, event_ts, created_at);
CREATE TABLE IF NOT EXISTS schwab_account_activity (
    event_key TEXT PRIMARY KEY,
    account_number TEXT,
    sequence_number BIGINT,
    message_type TEXT NOT NULL,
    broker_order_id BIGINT,
    order_status TEXT,
    event_ts BIGINT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_schwab_account_activity_order
    ON schwab_account_activity (broker_order_id, event_ts DESC)
    WHERE broker_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_schwab_account_activity_account_ts
    ON schwab_account_activity (account_number, event_ts DESC);
CREATE TABLE IF NOT EXISTS gdelt_event_mentions (
    event_id TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT,
    mention_ts BIGINT,
    sentence_id INTEGER,
    confidence DOUBLE PRECISION,
    document_tone DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, url)
);
CREATE INDEX IF NOT EXISTS idx_gdelt_event_mentions_event
    ON gdelt_event_mentions (event_id, confidence DESC NULLS LAST, mention_ts DESC NULLS LAST);
CREATE TABLE IF NOT EXISTS news_quality_issues (
    issue_key TEXT PRIMARY KEY,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    article_url TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    review_note TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_news_quality_issues_status_seen
    ON news_quality_issues (status, last_seen DESC);
CREATE TABLE IF NOT EXISTS historical_article_enrichments (
    url TEXT PRIMARY KEY,
    source TEXT,
    published_ts BIGINT,
    original_title TEXT,
    display_headline TEXT,
    relevance_label TEXT,
    relevance_score DOUBLE PRECISION,
    relevance_confidence DOUBLE PRECISION,
    verification_status TEXT NOT NULL,
    content_source TEXT,
    supporting_url TEXT,
    related_tickers JSONB NOT NULL DEFAULT '[]'::jsonb,
    related_companies JSONB NOT NULL DEFAULT '[]'::jsonb,
    company_relationships JSONB NOT NULL DEFAULT '[]'::jsonb,
    result JSONB NOT NULL,
    pipeline_version TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS historical_article_graph_outbox (
    url TEXT PRIMARY KEY,
    result JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_historical_article_graph_outbox_due
    ON historical_article_graph_outbox (next_attempt_at, url);
CREATE INDEX IF NOT EXISTS idx_historical_article_enrichments_published
    ON historical_article_enrichments (published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_historical_article_enrichments_relevance
    ON historical_article_enrichments (relevance_label, published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_historical_article_enrichments_review_order
    ON historical_article_enrichments (processed_at DESC, published_ts DESC)
    WHERE NOT (result ? 'processing_error');
CREATE INDEX IF NOT EXISTS idx_historical_article_enrichments_relationship_url
    ON historical_article_enrichments (url)
    WHERE jsonb_array_length(company_relationships) > 0;
CREATE TABLE IF NOT EXISTS news_enrichment_versions (
    version_key TEXT PRIMARY KEY,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    article_url TEXT,
    published_ts BIGINT NOT NULL DEFAULT 0,
    observed_ts BIGINT NOT NULL,
    pipeline_version TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_enrichment_versions_record_observed
    ON news_enrichment_versions (record_type, record_id, observed_ts DESC);
CREATE INDEX IF NOT EXISTS idx_news_enrichment_versions_url_observed
    ON news_enrichment_versions (article_url, observed_ts DESC)
    WHERE article_url IS NOT NULL;
CREATE TABLE IF NOT EXISTS market_observations (
    observation_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    observation_type TEXT NOT NULL,
    source TEXT NOT NULL,
    event_ts BIGINT NOT NULL,
    received_ts BIGINT NOT NULL,
    price DOUBLE PRECISION,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_observations_symbol_event
    ON market_observations (symbol, event_ts DESC, received_ts DESC);
CREATE INDEX IF NOT EXISTS idx_market_observations_received
    ON market_observations (received_ts DESC);
CREATE TABLE IF NOT EXISTS strategy_evaluation_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    acceptance_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    start_ts BIGINT NOT NULL,
    end_ts BIGINT NOT NULL,
    config JSONB NOT NULL,
    input_summary JSONB NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_evaluation_runs_symbol_created
    ON strategy_evaluation_runs (symbol, created_at DESC);
CREATE TABLE IF NOT EXISTS historical_article_retries (
    url TEXT PRIMARY KEY,
    article JSONB NOT NULL,
    pipeline_version TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_historical_article_retries_due
    ON historical_article_retries (next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS worker_checkpoints (
    name TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS news_quality_runs (
    id BIGSERIAL PRIMARY KEY,
    report JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS news_llm_validations (
    validation_key TEXT PRIMARY KEY,
    article_url TEXT NOT NULL,
    category TEXT NOT NULL,
    judgment TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    pipeline_version TEXT NOT NULL DEFAULT '',
    note TEXT,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_llm_validations_category_judgment
    ON news_llm_validations (category, judgment, updated_at DESC);
CREATE TABLE IF NOT EXISTS sentiment_benchmark_cases (
    case_key TEXT PRIMARY KEY,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    expected_label TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS sentiment_model_evaluations (
    evaluation_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    expected_label TEXT NOT NULL,
    predicted_label TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    probabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    correct BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE sentiment_model_evaluations
    ADD COLUMN IF NOT EXISTS case_key TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_sentiment_model_evaluations_model_updated
    ON sentiment_model_evaluations (model, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sentiment_model_evaluations_case
    ON sentiment_model_evaluations (case_key, model);
"""


def enabled() -> bool:
    return bool(DATABASE_URL and psycopg is not None)


def healthcheck() -> bool:
    """Verify that the durable store and trading journal schema are reachable."""
    if not enabled():
        return False
    with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.trade_positions') IS NOT NULL"
        ).fetchone()
    return bool(row and row[0])


def jsonb(value: Any):
    if psycopg is None:
        return value
    return psycopg.types.json.Jsonb(value)


def _news_enrichment_version(
    *,
    record_type: str,
    record_id: str,
    article_url: str | None,
    published_ts: int,
    pipeline_version: str,
    result: dict[str, Any],
    observed_ts: int | None = None,
) -> dict[str, Any]:
    normalized_url = str(article_url) if article_url else None
    normalized_published_ts = max(0, int(published_ts or 0))
    identity = {
        "record_type": str(record_type),
        "record_id": str(record_id),
        "article_url": normalized_url,
        "published_ts": normalized_published_ts,
        "pipeline_version": str(pipeline_version or "unknown"),
        "result": result,
    }
    return {
        **identity,
        "version_key": hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "observed_ts": int(observed_ts or time.time()),
    }


def _insert_news_enrichment_version(conn, version: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO news_enrichment_versions (
            version_key, record_type, record_id, article_url, published_ts,
            observed_ts, pipeline_version, result
        ) VALUES (
            %(version_key)s, %(record_type)s, %(record_id)s, %(article_url)s,
            %(published_ts)s, %(observed_ts)s, %(pipeline_version)s, %(result)s
        )
        ON CONFLICT (version_key) DO NOTHING
        """,
        {**version, "result": jsonb(version.get("result") or {})},
    )


def ensure_schema() -> None:
    global _SCHEMA_READY_FOR
    if not enabled():
        return
    key = (DATABASE_URL, SCHEMA)
    with _SCHEMA_LOCK:
        if _SCHEMA_READY_FOR == key:
            return
        with psycopg.connect(DATABASE_URL) as conn:
            # A waiting DDL lock can block ordinary readers/writers behind it.
            conn.execute("SET LOCAL lock_timeout = '5s'")
            # Serialize startup DDL across containers, never on every data read/write.
            conn.execute("SELECT pg_advisory_xact_lock(741852963)")
            digest = hashlib.sha256(SCHEMA.encode("utf-8")).hexdigest()
            checkpoint_exists = conn.execute(
                "SELECT to_regclass('worker_checkpoints') IS NOT NULL"
            ).fetchone()[0]
            previous = None
            if checkpoint_exists:
                previous = conn.execute(
                    "SELECT value->>'digest' FROM worker_checkpoints WHERE name = %s",
                    ("persistent_store_schema",),
                ).fetchone()
            if not previous or previous[0] != digest:
                prior_schema = SCHEMA.replace(_GRAPH_OUTBOX_SCHEMA, "", 1)
                only_graph_outbox_added = (
                    _GRAPH_OUTBOX_SCHEMA in SCHEMA and previous
                    and previous[0] == hashlib.sha256(prior_schema.encode("utf-8")).hexdigest()
                )
                conn.execute(_GRAPH_OUTBOX_SCHEMA if only_graph_outbox_added else SCHEMA)
                # Commit the marker with the DDL; a failed migration must be retried.
                conn.execute(
                    "INSERT INTO worker_checkpoints (name, value) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                    ("persistent_store_schema", jsonb({"digest": digest})),
                )
        _SCHEMA_READY_FOR = key


def load_scanner_settings() -> dict[str, Any] | None:
    if not enabled():
        return None
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT settings FROM scanner_settings WHERE id = 'default'"
        ).fetchone()
    return dict(row["settings"]) if row and isinstance(row.get("settings"), dict) else None


def save_scanner_settings(updates: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    merged = {**current, **updates}
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO scanner_settings (id, settings)
            VALUES ('default', %(settings)s)
            ON CONFLICT (id) DO UPDATE SET
                settings = EXCLUDED.settings,
                updated_at = NOW()
            """,
            {"settings": jsonb(merged)},
        )
    return merged


def replace_scanner_results(buckets: dict[str, dict[str, dict[str, Any]]]) -> None:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM scanner_results")
        for bucket, rows in buckets.items():
            for symbol, row in rows.items():
                conn.execute(
                    """
                    INSERT INTO scanner_results (symbol, bucket, score, rvol, ts, payload)
                    VALUES (%(symbol)s, %(bucket)s, %(score)s, %(rvol)s, %(ts)s, %(payload)s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        bucket = EXCLUDED.bucket,
                        score = EXCLUDED.score,
                        rvol = EXCLUDED.rvol,
                        ts = EXCLUDED.ts,
                        payload = EXCLUDED.payload,
                        updated_at = NOW()
                    """,
                    {
                        "symbol": str(symbol).upper(),
                        "bucket": bucket,
                        "score": _float_or_none(row.get("score")),
                        "rvol": _float_or_none(row.get("rvol") or row.get("avr")),
                        "ts": _int_or_none(row.get("ts")),
                        "payload": jsonb(row),
                    },
                )


def list_scanner_results(bucket: str | None = None, focus_only: bool = False) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    params: dict[str, Any] = {}
    clauses = []
    if bucket:
        clauses.append("bucket = %(bucket)s")
        params["bucket"] = bucket
    if focus_only:
        clauses.append("bucket IN ('priority', 'actionable')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT payload
            FROM scanner_results
            {where}
            ORDER BY score DESC NULLS LAST, rvol DESC NULLS LAST, ts DESC NULLS LAST
            """,
            params,
        ).fetchall()
    return [dict(row["payload"]) for row in rows if isinstance(row.get("payload"), dict)]


def count_scanner_results(bucket: str | None = None, focus_only: bool = False) -> int:
    if not enabled():
        return 0
    ensure_schema()
    params: dict[str, Any] = {}
    clauses = []
    if bucket:
        clauses.append("bucket = %(bucket)s")
        params["bucket"] = bucket
    if focus_only:
        clauses.append("bucket IN ('priority', 'actionable')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM scanner_results {where}", params).fetchone()
    return int(row[0] or 0)


def load_calendar_state(session_id: str) -> dict[str, Any] | None:
    if not enabled():
        return None
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT payload FROM calendar_states WHERE session_id = %(session_id)s",
            {"session_id": session_id},
        ).fetchone()
    return dict(row["payload"]) if row and isinstance(row.get("payload"), dict) else None


def save_calendar_state(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    next_payload = dict(payload)
    next_payload["updated_at"] = _now_ms()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO calendar_states (session_id, payload, updated_at)
            VALUES (%(session_id)s, %(payload)s, %(updated_at)s)
            ON CONFLICT (session_id) DO UPDATE SET
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """,
            {
                "session_id": session_id,
                "payload": jsonb(next_payload),
                "updated_at": next_payload["updated_at"],
            },
        )
    return next_payload


def upsert_calendar_events(events: list[dict[str, Any]]) -> int:
    if not enabled() or not events:
        return 0
    ensure_schema()
    rows = [event for event in events if event.get("id") and event.get("date") and event.get("title")]
    projected_rows = []
    for event in rows:
        details = event.get("details") or {}
        projected = dict(event)
        if details.get("calendar_kind") == "market_news":
            projected["title"] = details.get("display_headline") or event["title"]
            projected["symbols"] = details.get("related_tickers") or []
        projected_rows.append({
            **projected, "symbols": jsonb(projected.get("symbols") or []),
            "details": jsonb(details),
        })
    # Existing enrichment wins over raw refreshes, just as it does in the details merge.
    merged_details = """(EXCLUDED.details || (calendar_events.details - ARRAY[
        'summary', 'published_ts', 'provider', 'providers',
        'source_urls', 'date_basis', 'inferred_event_date'
    ]))"""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                f"""
                INSERT INTO calendar_events (
                    id, event_date, event_type, title, source, url, symbols,
                    confidence, extraction_source, details
                ) VALUES (
                    %(id)s, %(date)s, %(type)s, %(title)s, %(source)s, %(url)s,
                    %(symbols)s, %(confidence)s, %(extraction_source)s, %(details)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    event_date = EXCLUDED.event_date,
                    event_type = EXCLUDED.event_type,
                    title = CASE WHEN {merged_details}->>'calendar_kind' = 'market_news'
                        THEN COALESCE(NULLIF({merged_details}->>'display_headline', ''), EXCLUDED.title)
                        ELSE EXCLUDED.title END,
                    source = EXCLUDED.source,
                    url = EXCLUDED.url,
                    symbols = CASE WHEN {merged_details}->>'calendar_kind' = 'market_news'
                        THEN COALESCE({merged_details}->'related_tickers', '[]'::jsonb)
                        ELSE EXCLUDED.symbols END,
                    confidence = EXCLUDED.confidence,
                    extraction_source = EXCLUDED.extraction_source,
                    details = {merged_details},
                    updated_at = NOW()
                """,
                projected_rows,
            )
    return len(rows)


def list_calendar_events(limit: int = 5000, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        clauses = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 10000))}
        if start_date:
            clauses.append("event_date >= %(start_date)s")
            params["start_date"] = start_date
        if end_date:
            clauses.append("event_date <= %(end_date)s")
            params["end_date"] = end_date
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT id, event_date::text AS date, event_type AS type, title, source,
                   url, symbols, confidence, extraction_source, details
            FROM calendar_events
            {where}
            ORDER BY event_date DESC, title ASC
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]

def list_unclassified_market_news(limit: int = 500) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT id, event_date::text AS date, event_type AS type, title, source,
                   url, symbols, confidence, extraction_source, details
            FROM calendar_events
            WHERE details->>'calendar_kind' = 'market_news'
              AND COALESCE(details->>'relevance_label', '') = ''
            ORDER BY
                COALESCE((details->>'published_ts')::bigint, 0) DESC,
                event_date DESC,
                created_at DESC
            LIMIT %(limit)s
            """,
            {"limit": max(1, min(int(limit), 5000))},
        ).fetchall()
    return [dict(row) for row in rows]


def count_market_news_relevance_work() -> int:
    if not enabled():
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM calendar_events
            WHERE details->>'calendar_kind' = 'market_news'
              AND (
                  COALESCE(details->>'relevance_label', '') = ''
                  OR COALESCE(details->>'article_verification_status', '') = ''
              )
            """
        ).fetchone()
    return int(row[0] if row else 0)


def list_market_news_backfill_page(
    *,
    cutoff_ts: int,
    after_id: str = "",
    limit: int = 8,
) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT id, event_date::text AS date, event_type AS type, title, source,
                   url, symbols, confidence, extraction_source, details
            FROM calendar_events
            WHERE details->>'calendar_kind' = 'market_news'
              AND created_at <= to_timestamp(%(cutoff_ts)s)
              AND id > %(after_id)s
              AND (
                  COALESCE(details->>'relevance_label', '') = ''
                  OR COALESCE(details->>'article_verification_status', '') = ''
              )
            ORDER BY id
            LIMIT %(limit)s
            """,
            {
                "cutoff_ts": int(cutoff_ts),
                "after_id": str(after_id or ""),
                "limit": max(1, min(int(limit), 100)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def load_worker_checkpoint(name: str) -> dict[str, Any]:
    if not enabled():
        return {}
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT value FROM worker_checkpoints WHERE name = %(name)s",
            {"name": name},
        ).fetchone()
    return dict(row["value"]) if row and isinstance(row.get("value"), dict) else {}


def save_worker_checkpoint(name: str, value: dict[str, Any]) -> None:
    save_worker_checkpoints({name: value})


def save_worker_checkpoints(values: dict[str, dict[str, Any]]) -> None:
    """Commit related worker cursors together, without partial advancement."""
    if not values:
        return
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.cursor().executemany(
            """
            INSERT INTO worker_checkpoints (name, value)
            VALUES (%(name)s, %(value)s)
            ON CONFLICT (name) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            [{"name": name, "value": jsonb(value)} for name, value in values.items()],
        )


def upsert_historical_article_enrichment(
    article: dict[str, Any],
    result: dict[str, Any],
    pipeline_version: str,
    *, _connection=None,
) -> None:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    if _connection is None:
        ensure_schema()
    version = _news_enrichment_version(
        record_type="historical_article",
        record_id=str(article["url"]),
        article_url=str(article["url"]),
        published_ts=int(article.get("published_ts") or 0),
        observed_ts=int(time.time()),
        pipeline_version=pipeline_version,
        result=result,
    )
    with nullcontext(_connection) if _connection is not None else psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO historical_article_enrichments (
                url, source, published_ts, original_title, display_headline,
                relevance_label, relevance_score, relevance_confidence,
                verification_status, content_source, supporting_url,
                related_tickers, related_companies, company_relationships,
                result, pipeline_version
            ) VALUES (
                %(url)s, %(source)s, %(published_ts)s, %(original_title)s,
                %(display_headline)s, %(relevance_label)s, %(relevance_score)s,
                %(relevance_confidence)s, %(verification_status)s,
                %(content_source)s, %(supporting_url)s, %(related_tickers)s,
                %(related_companies)s, %(company_relationships)s, %(result)s,
                %(pipeline_version)s
            )
            ON CONFLICT (url) DO UPDATE SET
                source = EXCLUDED.source,
                published_ts = EXCLUDED.published_ts,
                original_title = EXCLUDED.original_title,
                display_headline = EXCLUDED.display_headline,
                relevance_label = EXCLUDED.relevance_label,
                relevance_score = EXCLUDED.relevance_score,
                relevance_confidence = EXCLUDED.relevance_confidence,
                verification_status = EXCLUDED.verification_status,
                content_source = EXCLUDED.content_source,
                supporting_url = EXCLUDED.supporting_url,
                related_tickers = EXCLUDED.related_tickers,
                related_companies = EXCLUDED.related_companies,
                company_relationships = EXCLUDED.company_relationships,
                result = EXCLUDED.result,
                pipeline_version = EXCLUDED.pipeline_version,
                processed_at = NOW(),
                updated_at = NOW()
            """,
            {
                "url": article["url"],
                "source": article.get("source"),
                "published_ts": int(article.get("published_ts") or 0),
                "original_title": article.get("original_title") or article.get("title") or "",
                "display_headline": result.get("display_headline") or article.get("title") or "",
                "relevance_label": result.get("relevance_label"),
                "relevance_score": result.get("relevance_score"),
                "relevance_confidence": result.get("relevance_confidence"),
                "verification_status": result.get("article_verification_status") or "unverified",
                "content_source": result.get("article_content_source"),
                "supporting_url": result.get("supporting_article_url"),
                "related_tickers": jsonb(result.get("related_tickers") or []),
                "related_companies": jsonb(result.get("related_companies") or []),
                "company_relationships": jsonb(result.get("company_relationships") or []),
                "result": jsonb(result),
                "pipeline_version": pipeline_version,
            },
        )
        _insert_news_enrichment_version(conn, version)


def save_historical_article_results(rows: list[tuple[dict, dict]], pipeline_version: str) -> None:
    """Commit inference and graph-delivery intent together, once per bounded group."""
    if not rows:
        return
    if len(rows) > 8:
        raise ValueError("Historical persistence batch exceeds eight records")
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    from services.news_rejection import receipt
    with psycopg.connect(DATABASE_URL) as conn:
        for article, result in rows:
            if result.get("processing_error") or result.get("relevance_source") == "processing_failure":
                raise ValueError("Cannot persist failed inference as completed")
            result = receipt(result)
            if result.get("rejected_receipt"):
                article = {"url": article["url"], "published_ts": article.get("published_ts", 0)}
            upsert_historical_article_enrichment(article, result, pipeline_version, _connection=conn)
            conn.execute(
                """INSERT INTO historical_article_graph_outbox (url, result)
                   VALUES (%s, %s) ON CONFLICT (url) DO UPDATE SET
                       result = EXCLUDED.result, attempts = 0,
                       next_attempt_at = NOW(), updated_at = NOW()""",
                (article["url"], jsonb({**result, "published_ts": int(article.get("published_ts") or 0)})),
            )
        conn.execute("DELETE FROM historical_article_retries WHERE url = ANY(%s)",
                     ([article["url"] for article, _ in rows],))


def historical_graph_backlog_full(limit: int = 1000) -> bool:
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        count = conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM historical_article_graph_outbox LIMIT %s) pending",
            (limit,),
        ).fetchone()[0]
    return count >= limit


def recover_historical_article_results(urls: list[str]) -> None:
    if not urls:
        return
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """INSERT INTO historical_article_graph_outbox (url, result)
               SELECT url, result || jsonb_build_object('published_ts', published_ts)
               FROM historical_article_enrichments
               WHERE url = ANY(%s) AND NOT (result ? 'processing_error')
               ON CONFLICT (url) DO NOTHING""", (urls,),
        )
        conn.execute("DELETE FROM historical_article_retries WHERE url = ANY(%s)", (urls,))


def deliver_historical_graph_result(deliver) -> str:
    """Lock only one outbox row; crash/partial graph writes remain replayable."""
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """SELECT url, result FROM historical_article_graph_outbox
               WHERE next_attempt_at <= NOW() ORDER BY next_attempt_at, url
               LIMIT 1 FOR UPDATE SKIP LOCKED"""
        ).fetchone()
        if not row:
            return "idle"
        try:
            if not deliver(row["url"], row["result"]):
                raise RuntimeError("Article graph delivery did not find the article")
        except Exception:
            conn.execute(
                """UPDATE historical_article_graph_outbox SET attempts = attempts + 1,
                   next_attempt_at = NOW() + INTERVAL '1 minute' WHERE url = %s""", (row["url"],),
            )
            return "retry"
        conn.execute("DELETE FROM historical_article_graph_outbox WHERE url = %s", (row["url"],))
        return "delivered"


def list_company_relationship_graph_page(after_url: str = "", limit: int = 500) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT url, published_ts, company_relationships
            FROM historical_article_enrichments
            WHERE url > %(after_url)s
              AND jsonb_array_length(company_relationships) > 0
            ORDER BY url
            LIMIT %(limit)s
            """,
            {
                "after_url": str(after_url or ""),
                "limit": max(1, min(int(limit), 2000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def enqueue_historical_article_retry(
    article: dict[str, Any],
    pipeline_version: str,
    error: str,
) -> None:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    url = str(article.get("url") or "").strip()
    if not url:
        return
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO historical_article_retries (
                url, article, pipeline_version, attempts, last_error,
                next_attempt_at
            ) VALUES (
                %(url)s, %(article)s, %(pipeline_version)s, 1, %(error)s,
                NOW() + INTERVAL '5 minutes'
            )
            ON CONFLICT (url) DO UPDATE SET
                article = EXCLUDED.article,
                pipeline_version = EXCLUDED.pipeline_version,
                attempts = historical_article_retries.attempts + 1,
                last_error = EXCLUDED.last_error,
                next_attempt_at = NOW() + make_interval(secs =>
                    LEAST(86400, 300 * power(2, LEAST(9, historical_article_retries.attempts)))::int),
                updated_at = NOW()
            """,
            {
                "url": url,
                "article": jsonb(article),
                "pipeline_version": pipeline_version,
                "error": str(error or "Unknown model failure")[:1000],
            },
        )


def resolve_historical_article_retry(url: str) -> None:
    if not enabled():
        return
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            "DELETE FROM historical_article_retries WHERE url = %(url)s",
            {"url": str(url or "")},
        )


def list_historical_article_retries(limit: int = 4) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT article, attempts, last_error
            FROM historical_article_retries
            WHERE next_attempt_at <= NOW()
            ORDER BY (article->>'published_ts')::bigint DESC NULLS LAST,
                     next_attempt_at,
                     created_at
            LIMIT %(limit)s
            """,
            {"limit": max(1, min(int(limit), 100))},
        ).fetchall()
    return [
        {
            **dict(row["article"]),
            "_retry_attempts": int(row.get("attempts") or 0),
            "_retry_error": row.get("last_error") or "",
        }
        for row in rows
        if isinstance(row.get("article"), dict)
    ]


def count_historical_article_retries() -> int:
    if not enabled():
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM historical_article_retries"
        ).fetchone()
    return int(row["count"] if row else 0)


class NewsQualityRefreshBusy(RuntimeError):
    """Another database transaction is already computing quality metrics."""


def news_llm_quality_summary() -> dict[str, Any]:
    """Return exact coverage metrics, allowing only one historical scan at a time."""
    if not enabled():
        return {"available": False, "reason": "PostgreSQL is not configured"}
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        # The Redis request lease may expire before a large report completes.
        # This transaction lock survives that lease and releases on failure too.
        lock = conn.execute(
            "SELECT pg_try_advisory_xact_lock(741852965) AS acquired"
        ).fetchone()
        if not lock or not lock["acquired"]:
            raise NewsQualityRefreshBusy("News quality metrics are being generated")
        # JSON expressions lack cardinality statistics: the planner otherwise
        # sorts wide article rows for the few label/source combinations.
        sort_setting = conn.execute("SHOW enable_sort").fetchone()["enable_sort"]
        conn.execute("SET LOCAL enable_sort = off")
        groups = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(relevance_label, ''), 'unknown') AS label,
                COALESCE(NULLIF(result->>'headline_source', ''), 'unknown') AS source,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE NOT (result ? 'processing_error')) AS completed,
                COUNT(*) FILTER (WHERE result ? 'processing_error') AS processing_errors,
                COUNT(*) FILTER (WHERE jsonb_array_length(related_tickers) > 0) AS with_tickers,
                COUNT(*) FILTER (WHERE jsonb_array_length(related_companies) > 0) AS with_companies,
                COUNT(*) FILTER (WHERE jsonb_array_length(company_relationships) > 0) AS with_relationships,
                COUNT(*) FILTER (WHERE verification_status = 'canonical') AS canonical_verified,
                COUNT(*) FILTER (
                    WHERE COALESCE(display_headline, '') <> ''
                      AND display_headline IS DISTINCT FROM original_title
                ) AS headline_changed,
                COUNT(*) FILTER (WHERE result->>'headline_source' = 'llm_translation') AS translated,
                COUNT(*) FILTER (
                    WHERE NOT (result ? 'processing_error')
                      AND COALESCE((result->>'entity_link_version')::integer, 0) >= 2
                      AND verification_status IN ('canonical', 'gdelt_mention')
                ) AS source_grounded,
                COUNT(*) FILTER (
                    WHERE NOT (result ? 'processing_error')
                      AND relevance_confidence >= 0.80
                ) AS confidence_passed,
                COUNT(*) FILTER (
                    WHERE NOT (result ? 'processing_error')
                      AND verification_status IN ('canonical', 'gdelt_mention')
                      AND relevance_confidence >= 0.80
                      AND relevance_label <> 'uncertain'
                ) AS automatically_accepted,
                COUNT(*) FILTER (
                    WHERE NOT (result ? 'processing_error')
                      AND relevance_label = 'uncertain'
                ) AS uncertain_records,
                MIN(published_ts) FILTER (WHERE published_ts > 0) AS oldest_published_ts,
                MAX(published_ts) FILTER (WHERE published_ts > 0) AS newest_published_ts,
                MAX(EXTRACT(EPOCH FROM processed_at))::bigint AS last_processed_ts
            FROM historical_article_enrichments
            GROUP BY 1, 2
            """
        ).fetchall()
        conn.execute("SELECT set_config('enable_sort', %s, true)", (sort_setting,))
        retries = conn.execute("SELECT COUNT(*) AS count FROM historical_article_retries").fetchone()
        validation_rows = conn.execute(
            """
            SELECT category, judgment, COUNT(*) AS count
            FROM news_llm_validations
            GROUP BY category, judgment
            ORDER BY category, judgment
            """
        ).fetchall()
    # Grouping preserves every count predicate while avoiding two extra table reads.
    values: dict[str, int] = {}
    labels: dict[str, int] = {}
    headline_sources: dict[str, int] = {}
    for group in groups:
        for key, value in group.items():
            if key in ("label", "source") or value is None:
                continue
            value = int(value)
            if key == "oldest_published_ts":
                values[key] = min(values.get(key, value), value)
            elif key in ("newest_published_ts", "last_processed_ts"):
                values[key] = max(values.get(key, value), value)
            else:
                values[key] = values.get(key, 0) + value
        completed_count = int(group["completed"])
        if completed_count:
            labels[group["label"]] = labels.get(group["label"], 0) + completed_count
            headline_sources[group["source"]] = headline_sources.get(group["source"], 0) + completed_count
    completed = int(values.get("completed") or 0)
    automatically_accepted = int(values.get("automatically_accepted") or 0)
    uncertain_records = int(values.get("uncertain_records") or 0)
    processing_errors = int(values.get("processing_errors") or 0)
    def rate(key: str) -> float:
        return round(int(values.get(key) or 0) * 100 / completed, 1) if completed else 0.0
    validation_by_category: dict[str, dict[str, Any]] = {
        category: {"correct": 0, "incorrect": 0}
        for category in NEWS_LLM_VALIDATION_CATEGORIES
    }
    validation_correct = validation_incorrect = 0
    for row in validation_rows:
        category = str(row["category"])
        count = int(row["count"] or 0)
        bucket = validation_by_category.setdefault(category, {"correct": 0, "incorrect": 0})
        if row["judgment"] == "correct":
            bucket["correct"] += count
            validation_correct += count
        elif row["judgment"] == "incorrect":
            bucket["incorrect"] += count
            validation_incorrect += count
    for bucket in validation_by_category.values():
        bucket["total"] = bucket["correct"] + bucket["incorrect"]
        bucket["accuracy_percent"] = round(bucket["correct"] * 100 / bucket["total"], 1) if bucket["total"] else None
        lower, upper = _wilson_interval(bucket["correct"], bucket["total"])
        bucket["confidence_95_lower_percent"] = lower
        bucket["confidence_95_upper_percent"] = upper
        bucket["target"] = NEWS_LLM_REVIEW_TARGET_PER_CATEGORY
        bucket["remaining_to_target"] = max(0, NEWS_LLM_REVIEW_TARGET_PER_CATEGORY - bucket["total"])
    validation_total = validation_correct + validation_incorrect
    validation_target = NEWS_LLM_REVIEW_TARGET_PER_CATEGORY * len(NEWS_LLM_VALIDATION_CATEGORIES)
    validation_lower, validation_upper = _wilson_interval(validation_correct, validation_total)
    return {
        "available": True,
        **{key: int(values.get(key) or 0) for key in (
            "total", "completed", "processing_errors", "with_tickers", "with_companies",
            "with_relationships", "canonical_verified", "headline_changed", "translated",
            "source_grounded", "confidence_passed", "automatically_accepted",
            "uncertain_records",
            "oldest_published_ts", "newest_published_ts", "last_processed_ts",
        )},
        "retry_pending": int(retries.get("count") or 0) if retries else 0,
        "coverage": {
            "tickers_percent": rate("with_tickers"),
            "companies_percent": rate("with_companies"),
            "relationships_percent": rate("with_relationships"),
            "canonical_verified_percent": rate("canonical_verified"),
            "headline_changed_percent": rate("headline_changed"),
            "translated_percent": rate("translated"),
        },
        "relevance_labels": dict(sorted(labels.items(), key=lambda item: (-item[1], item[0]))),
        "headline_sources": dict(sorted(headline_sources.items(), key=lambda item: (-item[1], item[0]))),
        "automated_acceptance": {
            "accepted": automatically_accepted,
            "provisional": max(0, completed - automatically_accepted - uncertain_records),
            "needs_attention": processing_errors + uncertain_records,
            "accepted_percent": round(automatically_accepted * 100 / completed, 1) if completed else 0.0,
            "source_grounded_percent": rate("source_grounded"),
            "confidence_passed_percent": rate("confidence_passed"),
            "ticker_validated_articles": int(values.get("with_tickers") or 0),
            "relationship_grounded_articles": int(values.get("with_relationships") or 0),
            "criteria": {
                "minimum_confidence": 0.80,
                "requires_grounded_source": True,
                "requires_certain_classification": True,
            },
        },
        "human_validation": {
            "total": validation_total,
            "correct": validation_correct,
            "incorrect": validation_incorrect,
            "accuracy_percent": round(validation_correct * 100 / validation_total, 1) if validation_total else None,
            "confidence_95_lower_percent": validation_lower,
            "confidence_95_upper_percent": validation_upper,
            "target": validation_target,
            "remaining_to_target": max(0, validation_target - validation_total),
            "categories_complete": sum(
                bucket["total"] >= NEWS_LLM_REVIEW_TARGET_PER_CATEGORY
                for bucket in validation_by_category.values()
            ),
            "categories_total": len(NEWS_LLM_VALIDATION_CATEGORIES),
            "by_category": validation_by_category,
        },
    }


def iter_accepted_news_training_records(
    *,
    ticker: str | None = None,
    start_ts: int = 0,
    end_ts: int = 0,
    limit: int = 250_000,
):
    """Stream grounded, ticker-linked News LLM records without loading them all in memory."""
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    clauses = ["row_number = 1"]
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 1_000_000))}
    normalized_ticker = str(ticker or "").strip().upper()
    if normalized_ticker:
        clauses.append("related_tickers ? %(ticker)s")
        params["ticker"] = normalized_ticker
    if int(start_ts or 0) > 0:
        clauses.append("published_ts >= %(start_ts)s")
        params["start_ts"] = int(start_ts)
    if int(end_ts or 0) > 0:
        clauses.append("published_ts <= %(end_ts)s")
        params["end_ts"] = int(end_ts)
    where = " AND ".join(clauses)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor(name="accepted_news_training_export") as cursor:
            cursor.itersize = 1000
            cursor.execute(
                f"""
                WITH candidates AS (
                    SELECT
                        COALESCE(NULLIF(result->>'resolved_article_url', ''), url) AS url,
                        source, published_ts, original_title, display_headline,
                        relevance_label, relevance_score, relevance_confidence,
                        verification_status, content_source, supporting_url,
                        related_tickers, related_companies, company_relationships,
                        result, pipeline_version, processed_at,
                        'historical'::text AS record_source,
                        1 AS source_priority
                    FROM historical_article_enrichments
                    WHERE NOT (result ? 'processing_error')
                      AND COALESCE((result->>'entity_link_version')::integer, 0) >= 2
                      AND verification_status IN ('canonical', 'gdelt_mention')
                      AND relevance_confidence >= 0.80
                      AND relevance_label IN ('finance', 'business', 'markets')
                      AND jsonb_array_length(related_tickers) > 0

                    UNION ALL

                    SELECT
                        COALESCE(NULLIF(details->>'resolved_article_url', ''), url) AS url,
                        source,
                        COALESCE((details->>'published_ts')::bigint, 0) AS published_ts,
                        COALESCE(NULLIF(details->>'original_headline', ''), title) AS original_title,
                        COALESCE(NULLIF(details->>'display_headline', ''), title) AS display_headline,
                        details->>'relevance_label' AS relevance_label,
                        COALESCE((details->>'relevance_score')::double precision, 0) AS relevance_score,
                        COALESCE((details->>'relevance_confidence')::double precision, 0) AS relevance_confidence,
                        COALESCE(details->>'article_verification_status', 'unverified') AS verification_status,
                        details->>'article_content_source' AS content_source,
                        details->>'supporting_article_url' AS supporting_url,
                        COALESCE(details->'related_tickers', '[]'::jsonb) AS related_tickers,
                        COALESCE(details->'related_companies', '[]'::jsonb) AS related_companies,
                        COALESCE(details->'company_relationships', '[]'::jsonb) AS company_relationships,
                        details AS result,
                        COALESCE(NULLIF(details->>'pipeline_version', ''), 'live') AS pipeline_version,
                        updated_at AS processed_at,
                        'live'::text AS record_source,
                        0 AS source_priority
                    FROM calendar_events
                    WHERE details->>'calendar_kind' = 'market_news'
                      AND NOT (details ? 'processing_error')
                      AND COALESCE((details->>'entity_link_version')::integer, 0) >= 2
                      AND details->>'article_verification_status' IN ('canonical', 'gdelt_mention')
                      AND COALESCE((details->>'relevance_confidence')::double precision, 0) >= 0.80
                      AND details->>'relevance_label' IN ('finance', 'business', 'markets')
                      AND jsonb_array_length(COALESCE(details->'related_tickers', '[]'::jsonb)) > 0
                ), ranked AS (
                    SELECT candidates.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY url
                               ORDER BY source_priority, processed_at DESC
                           ) AS row_number
                    FROM candidates
                    WHERE url IS NOT NULL AND url <> ''
                )
                SELECT url, source, published_ts, original_title, display_headline,
                       relevance_label, relevance_score, relevance_confidence,
                       verification_status, content_source, supporting_url,
                       related_tickers, related_companies, company_relationships,
                       result, pipeline_version, processed_at, record_source
                FROM ranked
                WHERE {where}
                ORDER BY published_ts DESC, url DESC
                LIMIT %(limit)s
                """,
                params,
            )
            for row in cursor:
                row_data = dict(row)
                result = dict(row_data.pop("result") or {})
                yield {
                    "schema_version": 1,
                    **row_data,
                    "summary": result.get("llm_summary") or "",
                    "topics": result.get("llm_topics") or [],
                    "commodities": result.get("related_commodities") or [],
                    "language": result.get("source_language") or "",
                    "relevance_reason": result.get("relevance_reason") or "",
                    "headline_source": result.get("headline_source") or "",
                    "model": result.get("relevance_model") or "",
                }


def count_news_entity_relink_remaining(entity_link_version: int) -> int:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM historical_article_enrichments
            WHERE NOT (result ? 'processing_error')
              AND COALESCE((result->>'entity_link_version')::integer, 0) < %(version)s
            """,
            {"version": int(entity_link_version)},
        ).fetchone()
    return int(row["count"] if row else 0)


def list_news_entity_relink_page(
    *,
    entity_link_version: int,
    before_published_ts: int = 0,
    before_url: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT url, published_ts, related_tickers, result
            FROM historical_article_enrichments
            WHERE NOT (result ? 'processing_error')
              AND COALESCE((result->>'entity_link_version')::integer, 0) < %(version)s
              AND (
                    %(before_published_ts)s = 0
                    OR published_ts < %(before_published_ts)s
                    OR (published_ts = %(before_published_ts)s AND url < %(before_url)s)
              )
            ORDER BY published_ts DESC, url DESC
            LIMIT %(limit)s
            """,
            {
                "version": int(entity_link_version),
                "before_published_ts": max(0, int(before_published_ts or 0)),
                "before_url": str(before_url or ""),
                "limit": max(1, min(int(limit), 5000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def update_news_entity_links(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    updated = 0
    with psycopg.connect(DATABASE_URL) as conn:
        for item in rows:
            result_payload = dict(item.get("result") or {})
            result = conn.execute(
                """
                UPDATE historical_article_enrichments
                SET related_tickers = %(related_tickers)s,
                    related_companies = %(related_companies)s,
                    company_relationships = %(company_relationships)s,
                    result = %(result)s,
                    updated_at = NOW()
                WHERE url = %(url)s
                RETURNING url, published_ts, pipeline_version, result
                """,
                {
                    "url": item["url"],
                    "related_tickers": jsonb(item.get("related_tickers") or []),
                    "related_companies": jsonb(item.get("related_companies") or []),
                    "company_relationships": jsonb(item.get("company_relationships") or []),
                    "result": jsonb(result_payload),
                },
            ).fetchone()
            if not result:
                continue
            updated += 1
            _insert_news_enrichment_version(
                conn,
                _news_enrichment_version(
                    record_type="historical_article",
                    record_id=str(result[0]),
                    article_url=str(result[0]),
                    published_ts=int(result[1] or 0),
                    pipeline_version=str(result[2] or "unknown"),
                    result=dict(result[3] or {}),
                ),
            )
    return updated


def historical_enrichment_results_by_url(urls: list[str]) -> dict[str, dict[str, Any]]:
    normalized = sorted({str(url or "").strip() for url in urls if str(url or "").strip()})
    if not normalized:
        return {}
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT url, result
            FROM historical_article_enrichments
            WHERE url = ANY(%(urls)s)
              AND NOT (result ? 'processing_error')
            """,
            {"urls": normalized},
        ).fetchall()
    return {str(row["url"]): dict(row["result"] or {}) for row in rows}


def list_news_enrichment_versions_as_of(
    *,
    as_of_ts: int,
    urls: list[str] | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return only the latest enrichment revision observed by the cutoff."""
    if not enabled():
        return []
    cutoff = max(0, int(as_of_ts or 0))
    if cutoff <= 0:
        return []
    normalized = sorted({str(url).strip() for url in urls or [] if str(url).strip()})
    ensure_schema()
    params: dict[str, Any] = {
        "as_of_ts": cutoff,
        "limit": max(1, min(int(limit), 5000)),
    }
    url_clause = ""
    if normalized:
        params["urls"] = normalized[:1000]
        url_clause = "AND article_url = ANY(%(urls)s)"
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM (
            SELECT DISTINCT ON (record_type, record_id)
                   version_key, record_type, record_id, article_url,
                   published_ts, observed_ts, pipeline_version, result
            FROM news_enrichment_versions
            WHERE observed_ts <= %(as_of_ts)s
              AND published_ts <= %(as_of_ts)s
              {url_clause}
            ORDER BY record_type, record_id, observed_ts DESC, created_at DESC
            ) AS known_versions
            ORDER BY observed_ts DESC, record_type, record_id
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def seed_news_enrichment_version_baseline() -> int:
    """Conservatively snapshot current legacy enrichments at their last update time."""
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        historical = conn.execute(
            """
            INSERT INTO news_enrichment_versions (
                version_key, record_type, record_id, article_url, published_ts,
                observed_ts, pipeline_version, result
            )
            SELECT
                'baseline:historical:' || md5(
                    url || '|' || pipeline_version || '|' || result::text
                ),
                'historical_article', url, url, GREATEST(0, COALESCE(published_ts, 0)),
                EXTRACT(EPOCH FROM updated_at)::bigint, pipeline_version, result
            FROM historical_article_enrichments
            WHERE NOT (result ? 'processing_error')
            ON CONFLICT (version_key) DO NOTHING
            """
        )
        calendar = conn.execute(
            """
            INSERT INTO news_enrichment_versions (
                version_key, record_type, record_id, article_url, published_ts,
                observed_ts, pipeline_version, result
            )
            SELECT
                'baseline:calendar:' || md5(
                    id || '|' || COALESCE(details->>'pipeline_version', 'live')
                    || '|' || details::text
                ),
                'calendar_event', id, url,
                GREATEST(0, COALESCE(NULLIF(details->>'published_ts', '')::bigint, 0)),
                EXTRACT(EPOCH FROM updated_at)::bigint,
                COALESCE(NULLIF(details->>'pipeline_version', ''), 'live'), details
            FROM calendar_events
            WHERE details->>'calendar_kind' = 'market_news'
              AND COALESCE(details->>'relevance_label', '') <> ''
            ON CONFLICT (version_key) DO NOTHING
            """
        )
    return int(historical.rowcount or 0) + int(calendar.rowcount or 0)


def record_market_observations(
    rows: list[dict[str, Any]], *, ensure: bool = True
) -> int:
    """Store immutable event-time observations; duplicate payloads are idempotent."""
    if not rows:
        return 0
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    if ensure:
        ensure_schema()
    params = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        observation_type = str(row.get("observation_type") or "").strip().lower()
        source = str(row.get("source") or "").strip().lower()
        event_ts = int(row.get("event_ts") or 0)
        received_ts = int(row.get("received_ts") or 0)
        if not symbol or not observation_type or not source or event_ts <= 0 or received_ts <= 0:
            continue
        payload = dict(row.get("payload") or {})
        identity = {
            "symbol": symbol,
            "observation_type": observation_type,
            "source": source,
            "event_ts": event_ts,
            "received_ts": received_ts,
            "payload": payload,
        }
        params.append({
            **identity,
            "observation_key": hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "price": _float_or_none(row.get("price")),
            "bid": _float_or_none(row.get("bid")),
            "ask": _float_or_none(row.get("ask")),
            "open": _float_or_none(row.get("open")),
            "high": _float_or_none(row.get("high")),
            "low": _float_or_none(row.get("low")),
            "close": _float_or_none(row.get("close")),
            "volume": _float_or_none(row.get("volume")),
            "payload": jsonb(payload),
        })
    if not params:
        return 0
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO market_observations (
                    observation_key, symbol, observation_type, source,
                    event_ts, received_ts, price, bid, ask, open, high, low,
                    close, volume, payload
                ) VALUES (
                    %(observation_key)s, %(symbol)s, %(observation_type)s,
                    %(source)s, %(event_ts)s, %(received_ts)s, %(price)s,
                    %(bid)s, %(ask)s, %(open)s, %(high)s, %(low)s, %(close)s,
                    %(volume)s, %(payload)s
                )
                ON CONFLICT (observation_key) DO NOTHING
                """,
                params,
            )
    return len(params)


def list_market_observations_as_of(
    symbol: str,
    *,
    as_of_ts: int,
    start_ts: int = 0,
    observation_type: str = "minute_candle",
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Return the latest revision of each event that had arrived by the cutoff."""
    if not enabled():
        return []
    cutoff = max(0, int(as_of_ts or 0))
    if cutoff <= 0:
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (event_ts)
                       observation_key, symbol, observation_type, source,
                       event_ts, received_ts, price, bid, ask, open, high, low,
                       close, volume, payload
                FROM market_observations
                WHERE symbol = %(symbol)s
                  AND observation_type = %(observation_type)s
                  AND event_ts BETWEEN %(start_ts)s AND %(as_of_ts)s
                  AND received_ts <= %(as_of_ts)s
                ORDER BY event_ts, received_ts DESC, created_at DESC
            ) AS known_observations
            ORDER BY event_ts
            LIMIT %(limit)s
            """,
            {
                "symbol": str(symbol).upper(),
                "observation_type": str(observation_type).lower(),
                "start_ts": max(0, int(start_ts or 0)),
                "as_of_ts": cutoff,
                "limit": max(1, min(int(limit), 20_000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def list_market_observation_versions(
    symbol: str,
    *,
    start_ts: int,
    end_ts: int,
    observation_type: str | None = "minute_candle",
    limit: int = 100_000,
) -> list[dict[str, Any]]:
    """Return raw revisions so replay can enforce receipt-time availability."""
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        type_clause = (
            "AND observation_type = %(observation_type)s"
            if observation_type
            else ""
        )
        rows = conn.execute(
            f"""
            SELECT observation_key, symbol, observation_type, source,
                   event_ts, received_ts, price, bid, ask, open, high, low,
                   close, volume, payload
            FROM market_observations
            WHERE symbol = %(symbol)s
              {type_clause}
              AND event_ts BETWEEN %(start_ts)s AND %(end_ts)s
              AND received_ts <= %(end_ts)s
            ORDER BY event_ts, received_ts
            LIMIT %(limit)s
            """,
            {
                "symbol": str(symbol).upper(),
                "observation_type": str(observation_type).lower() if observation_type else None,
                "start_ts": max(0, int(start_ts)),
                "end_ts": max(0, int(end_ts)),
                "limit": max(1, min(int(limit), 100_000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def list_feature_signals_for_replay(
    symbol: str, *, start_ts: int, end_ts: int, limit: int = 5000
) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT payload
            FROM feature_store
            WHERE symbol = %(symbol)s
              AND ts BETWEEN %(start_ts)s AND %(end_ts)s
              AND semantic_signal->'hype'->>'feature_version' = 'news-hype-v1'
            ORDER BY ts
            LIMIT %(limit)s
            """,
            {
                "symbol": str(symbol).upper(),
                "start_ts": max(0, int(start_ts)),
                "end_ts": max(0, int(end_ts)),
                "limit": max(1, min(int(limit), 5000)),
            },
        ).fetchall()
    return [dict(row["payload"] or {}) for row in rows]


def record_strategy_evaluation_run(result: dict[str, Any]) -> str:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    identity = {
        "mode": result.get("mode"),
        "strategy_version": result.get("replay_version"),
        "acceptance_version": result.get("acceptance_version"),
        "symbol": result.get("symbol"),
        "start_ts": result.get("start_ts"),
        "end_ts": result.get("end_ts"),
        "config": result.get("config") or {},
        "audit": result.get("audit") or {},
        "metrics": result.get("metrics") or {},
    }
    run_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    input_summary = {
        key: value
        for key, value in (result.get("audit") or {}).items()
        if key.startswith("input_") or key == "eligible_signals"
    }
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO strategy_evaluation_runs (
                run_id, mode, strategy_version, acceptance_version, symbol,
                start_ts, end_ts, config, input_summary, result
            ) VALUES (
                %(run_id)s, %(mode)s, %(strategy_version)s,
                %(acceptance_version)s, %(symbol)s, %(start_ts)s, %(end_ts)s,
                %(config)s, %(input_summary)s, %(result)s
            )
            ON CONFLICT (run_id) DO NOTHING
            """,
            {
                "run_id": run_id,
                "mode": str(result.get("mode") or "historical_replay"),
                "strategy_version": str(result.get("replay_version") or "unknown"),
                "acceptance_version": str(result.get("acceptance_version") or "unknown"),
                "symbol": str(result.get("symbol") or "").upper(),
                "start_ts": int(result.get("start_ts") or 0),
                "end_ts": int(result.get("end_ts") or 0),
                "config": jsonb(result.get("config") or {}),
                "input_summary": jsonb(input_summary),
                "result": jsonb(result),
            },
        )
    return run_id


def list_strategy_evaluation_runs(
    *, symbol: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
    where = ""
    if symbol:
        where = "WHERE symbol = %(symbol)s"
        params["symbol"] = str(symbol).upper()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT run_id, mode, strategy_version, acceptance_version, symbol,
                   start_ts, end_ts, config, input_summary, result, created_at
            FROM strategy_evaluation_runs
            {where}
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
    return [
        {**dict(row), "created_at": row["created_at"].isoformat()}
        for row in rows
    ]


def calendar_enrichment_results_by_url(urls: list[str]) -> dict[str, dict[str, Any]]:
    normalized = sorted({str(url or "").strip() for url in urls if str(url or "").strip()})
    if not normalized:
        return {}
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT url, details
            FROM calendar_events
            WHERE details->>'calendar_kind' = 'market_news'
              AND NOT (details ? 'processing_error')
              AND (
                    url = ANY(%(urls)s)
                    OR details->>'resolved_article_url' = ANY(%(urls)s)
                    OR details->>'supporting_article_url' = ANY(%(urls)s)
              )
            """,
            {"urls": normalized},
        ).fetchall()
    results = {}
    for row in rows:
        details = dict(row["details"] or {})
        for value in (
            row["url"],
            details.get("resolved_article_url"),
            details.get("supporting_article_url"),
        ):
            if value:
                results[str(value)] = details
    return results


def list_calendar_entity_relink_page(
    *,
    entity_link_version: int,
    after_id: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT id, url, details
            FROM calendar_events
            WHERE details->>'calendar_kind' = 'market_news'
              AND NOT (details ? 'processing_error')
              AND COALESCE((details->>'entity_link_version')::integer, 0) < %(version)s
              AND id > %(after_id)s
            ORDER BY id
            LIMIT %(limit)s
            """,
            {
                "version": int(entity_link_version),
                "after_id": str(after_id or ""),
                "limit": max(1, min(int(limit), 5000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def update_calendar_entity_links(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    updated = 0
    with psycopg.connect(DATABASE_URL) as conn:
        for item in rows:
            result = conn.execute(
                """
                UPDATE calendar_events
                SET symbols = %(symbols)s,
                    details = details || %(details)s,
                    updated_at = NOW()
                WHERE id = %(id)s
                RETURNING id, url, details
                """,
                {
                    "id": item["id"],
                    "symbols": jsonb(item.get("related_tickers") or []),
                    "details": jsonb(item.get("result") or {}),
                },
            ).fetchone()
            if not result:
                continue
            updated += 1
            details = dict(result[2] or {})
            _insert_news_enrichment_version(
                conn,
                _news_enrichment_version(
                    record_type="calendar_event",
                    record_id=str(result[0]),
                    article_url=str(result[1]) if result[1] else None,
                    published_ts=int(details.get("published_ts") or 0),
                    pipeline_version=str(details.get("pipeline_version") or "live"),
                    result=details,
                ),
            )
    return updated


def news_llm_review_samples(per_category: int = 2) -> list[dict[str, Any]]:
    """Return recent examples across categories for manual quality validation."""
    if not enabled():
        return []
    ensure_schema()
    limit = max(1, min(int(per_category), 5))
    categories = {
        "ticker": "jsonb_array_length(related_tickers) > 0",
        "relationship": "jsonb_array_length(company_relationships) > 0",
        "translated": "result->>'headline_source' = 'llm_translation'",
        "headline_changed": "COALESCE(display_headline, '') <> '' AND display_headline IS DISTINCT FROM original_title",
        "unrelated": "relevance_label = 'unrelated'",
        "unverified": "verification_status = 'unverified'",
    }
    samples = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        for category, predicate in categories.items():
            candidate_limit = limit * 20
            rows = conn.execute(
                f"""
                SELECT h.url, h.source, h.published_ts, h.original_title, h.display_headline,
                       relevance_label, relevance_confidence, verification_status,
                       related_tickers, related_companies, company_relationships,
                       result->>'llm_summary' AS llm_summary,
                       result->>'relevance_reason' AS relevance_reason,
                       result->'llm_topics' AS llm_topics,
                       result->'related_commodities' AS related_commodities,
                       result->>'headline_source' AS headline_source,
                       result->>'relevance_model' AS relevance_model,
                       pipeline_version
                FROM historical_article_enrichments h
                WHERE NOT (result ? 'processing_error') AND ({predicate})
                  AND NOT EXISTS (
                      SELECT 1 FROM news_llm_validations v
                      WHERE v.article_url = h.url
                        AND v.category = %(category)s
                        AND v.model = COALESCE(h.result->>'relevance_model', '')
                        AND v.pipeline_version = h.pipeline_version
                  )
                ORDER BY processed_at DESC, published_ts DESC NULLS LAST
                LIMIT %(candidate_limit)s
                """,
                {"candidate_limit": candidate_limit, "category": category},
            ).fetchall()
            selected = []
            deferred = []
            seen_sources = set()
            for row in rows:
                source_key = str(row.get("source") or row.get("url") or "").strip().lower()
                if source_key and source_key not in seen_sources:
                    selected.append(row)
                    seen_sources.add(source_key)
                else:
                    deferred.append(row)
                if len(selected) >= limit:
                    break
            if len(selected) < limit:
                selected.extend(deferred[:limit - len(selected)])
            samples.extend({"category": category, **dict(row)} for row in selected)
    return samples


def review_news_llm_sample(
    article_url: str,
    category: str,
    judgment: str,
    note: str = "",
) -> dict[str, Any] | None:
    allowed_categories = set(NEWS_LLM_VALIDATION_CATEGORIES)
    if category not in allowed_categories or judgment not in {"correct", "incorrect"}:
        raise ValueError("Invalid News LLM validation category or judgment")
    if not enabled():
        return None
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT url, source, published_ts, original_title, display_headline,
                   relevance_label, relevance_confidence, verification_status,
                   related_tickers, related_companies, company_relationships,
                   result->>'llm_summary' AS llm_summary,
                   result->>'relevance_reason' AS relevance_reason,
                   result->'llm_topics' AS llm_topics,
                   result->'related_commodities' AS related_commodities,
                   result->>'headline_source' AS headline_source,
                   COALESCE(result->>'relevance_model', '') AS relevance_model,
                   pipeline_version
            FROM historical_article_enrichments
            WHERE url = %(url)s AND NOT (result ? 'processing_error')
            """,
            {"url": article_url},
        ).fetchone()
        if not row:
            return None
        snapshot = dict(row)
        model = str(snapshot.get("relevance_model") or "")
        pipeline_version = str(snapshot.get("pipeline_version") or "")
        validation_key = hashlib.sha256(
            f"{category}|{article_url}|{model}|{pipeline_version}".encode("utf-8")
        ).hexdigest()
        saved = conn.execute(
            """
            INSERT INTO news_llm_validations (
                validation_key, article_url, category, judgment, model,
                pipeline_version, note, snapshot
            ) VALUES (
                %(validation_key)s, %(article_url)s, %(category)s, %(judgment)s,
                %(model)s, %(pipeline_version)s, %(note)s, %(snapshot)s
            )
            ON CONFLICT (validation_key) DO UPDATE SET
                judgment = EXCLUDED.judgment,
                note = EXCLUDED.note,
                snapshot = EXCLUDED.snapshot,
                updated_at = NOW()
            RETURNING validation_key, article_url, category, judgment, model,
                      pipeline_version, note,
                      EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts
            """,
            {
                "validation_key": validation_key,
                "article_url": article_url,
                "category": category,
                "judgment": judgment,
                "model": model,
                "pipeline_version": pipeline_version,
                "note": str(note or "")[:1000],
                "snapshot": jsonb(snapshot),
            },
        ).fetchone()
    return dict(saved) if saved else None


def _normalize_sentiment_benchmark_case(case: dict[str, Any]) -> dict[str, str]:
    expected = str(case.get("expected_label") or "").strip().lower()
    if expected not in {"positive", "neutral", "negative"}:
        raise ValueError("Expected sentiment must be positive, neutral, or negative")
    normalized_headline = " ".join(str(case.get("headline") or "").split())[:1000]
    normalized_summary = " ".join(str(case.get("summary") or "").split())[:10000]
    if not normalized_headline:
        raise ValueError("A benchmark headline is required")
    case_key = hashlib.sha256(
        f"{normalized_headline}|{normalized_summary}".encode("utf-8")
    ).hexdigest()
    return {
        "case_key": case_key,
        "headline": normalized_headline,
        "summary": normalized_summary,
        "expected_label": expected,
        "source_url": str(case.get("source_url") or "")[:2000],
        "note": str(case.get("note") or "")[:1000],
    }


def save_sentiment_benchmark_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    normalized = {
        row["case_key"]: row
        for row in (_normalize_sentiment_benchmark_case(case) for case in cases)
    }
    if not normalized:
        return []
    if not enabled():
        return None
    ensure_schema()
    saved: list[dict[str, Any]] = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        for values in normalized.values():
            row = conn.execute(
                """
                INSERT INTO sentiment_benchmark_cases (
                    case_key, headline, summary, expected_label, source_url, note
                ) VALUES (
                    %(case_key)s, %(headline)s, %(summary)s, %(expected_label)s,
                    %(source_url)s, %(note)s
                )
                ON CONFLICT (case_key) DO UPDATE SET
                    expected_label = EXCLUDED.expected_label,
                    source_url = EXCLUDED.source_url,
                    note = EXCLUDED.note,
                    updated_at = NOW()
                RETURNING case_key, headline, summary, expected_label, source_url, note,
                          EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts
                """,
                values,
            ).fetchone()
            if row:
                saved.append(dict(row))
    return saved


def save_sentiment_benchmark_case(
    headline: str,
    summary: str,
    expected_label: str,
    source_url: str = "",
    note: str = "",
) -> dict[str, Any] | None:
    saved = save_sentiment_benchmark_cases([{
        "headline": headline,
        "summary": summary,
        "expected_label": expected_label,
        "source_url": source_url,
        "note": note,
    }])
    if saved is None:
        return None
    return saved[0] if saved else None


def list_sentiment_benchmark_cases(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT case_key, headline, summary, expected_label, source_url, note,
                   EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts
            FROM sentiment_benchmark_cases
            ORDER BY updated_at DESC, case_key
            LIMIT %(limit)s
            OFFSET %(offset)s
            """,
            {
                "limit": max(1, min(int(limit), 500)),
                "offset": max(0, int(offset)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def delete_sentiment_benchmark_case(case_key: str) -> bool:
    key = str(case_key or "").strip()
    if not key or not enabled():
        return False
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            "DELETE FROM sentiment_model_evaluations WHERE case_key = %(case_key)s",
            {"case_key": key},
        )
        deleted = conn.execute(
            "DELETE FROM sentiment_benchmark_cases WHERE case_key = %(case_key)s",
            {"case_key": key},
        ).rowcount
    return bool(deleted)


def save_sentiment_evaluation(
    headline: str,
    summary: str,
    expected_label: str,
    prediction: dict[str, Any],
) -> dict[str, Any] | None:
    predicted = str(prediction.get("label") or "").strip().lower()
    model = str(prediction.get("model") or "").strip()
    if predicted not in {"positive", "neutral", "negative"} or not model:
        raise ValueError("The sentiment model returned an invalid labeled prediction")
    case = save_sentiment_benchmark_case(headline, summary, expected_label)
    if not case:
        return None
    expected = str(case["expected_label"])
    evaluation_key = hashlib.sha256(f"{model}|{case['case_key']}".encode("utf-8")).hexdigest()
    values = {
        "evaluation_key": evaluation_key,
        "case_key": case["case_key"],
        "model": model,
        "headline": case["headline"],
        "summary": case["summary"],
        "expected_label": expected,
        "predicted_label": predicted,
        "confidence": max(0.0, min(float(prediction.get("confidence") or 0), 1.0)),
        "score": max(-1.0, min(float(prediction.get("score") or 0), 1.0)),
        "probabilities": jsonb(prediction.get("probabilities") or {}),
        "correct": expected == predicted,
    }
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            INSERT INTO sentiment_model_evaluations (
                evaluation_key, case_key, model, headline, summary, expected_label,
                predicted_label, confidence, score, probabilities, correct
            ) VALUES (
                %(evaluation_key)s, %(case_key)s, %(model)s, %(headline)s, %(summary)s,
                %(expected_label)s, %(predicted_label)s, %(confidence)s,
                %(score)s, %(probabilities)s, %(correct)s
            )
            ON CONFLICT (evaluation_key) DO UPDATE SET
                predicted_label = EXCLUDED.predicted_label,
                confidence = EXCLUDED.confidence,
                score = EXCLUDED.score,
                probabilities = EXCLUDED.probabilities,
                correct = EXCLUDED.correct,
                updated_at = NOW()
            RETURNING evaluation_key, case_key, model, headline, summary, expected_label,
                      predicted_label, confidence, score, probabilities, correct,
                      EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts
            """,
            values,
        ).fetchone()
    return dict(row) if row else None


def sentiment_evaluation_summary(limit: int = 20) -> dict[str, Any]:
    if not enabled():
        return {"available": False, "total": 0, "case_total": 0, "case_labels": {}, "models": [], "recent_cases": [], "recent": []}
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        aggregate = conn.execute(
            """
            SELECT model, COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE correct) AS correct,
                   AVG(confidence) AS mean_confidence
            FROM sentiment_model_evaluations
            GROUP BY model ORDER BY MAX(updated_at) DESC
            """
        ).fetchall()
        labels = conn.execute(
            """
            SELECT model, expected_label, predicted_label, COUNT(*) AS count
            FROM sentiment_model_evaluations
            GROUP BY model, expected_label, predicted_label
            ORDER BY model, expected_label, predicted_label
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT evaluation_key, model, headline, expected_label, predicted_label,
                   confidence, score, correct,
                   EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts
            FROM sentiment_model_evaluations
            ORDER BY updated_at DESC
            LIMIT %(limit)s
            """,
            {"limit": max(1, min(int(limit), 100))},
        ).fetchall()
        case_total = conn.execute("SELECT COUNT(*) AS count FROM sentiment_benchmark_cases").fetchone()
        case_labels = conn.execute(
            """
            SELECT expected_label, COUNT(*) AS count
            FROM sentiment_benchmark_cases
            GROUP BY expected_label ORDER BY expected_label
            """
        ).fetchall()
        recent_cases = conn.execute(
            """
            SELECT case_key, headline, summary, expected_label, source_url, note,
                   EXTRACT(EPOCH FROM updated_at)::bigint AS updated_ts
            FROM sentiment_benchmark_cases
            ORDER BY updated_at DESC
            LIMIT %(limit)s
            """,
            {"limit": max(1, min(int(limit), 100))},
        ).fetchall()
    by_model = {
        str(row["model"]): {
            "model": str(row["model"]),
            "total": int(row["total"] or 0),
            "correct": int(row["correct"] or 0),
            "accuracy_percent": round(int(row["correct"] or 0) * 100 / int(row["total"]), 1),
            "mean_confidence_percent": round(float(row["mean_confidence"] or 0) * 100, 1),
            "confusion": {},
        }
        for row in aggregate
        if int(row["total"] or 0) > 0
    }
    for row in labels:
        model = str(row["model"])
        if model in by_model:
            expected = str(row["expected_label"])
            predicted = str(row["predicted_label"])
            by_model[model]["confusion"].setdefault(expected, {})[predicted] = int(row["count"] or 0)
    return {
        "available": True,
        "total": sum(model["total"] for model in by_model.values()),
        "case_total": int(case_total["count"] or 0) if case_total else 0,
        "case_labels": {str(row["expected_label"]): int(row["count"] or 0) for row in case_labels},
        "models": list(by_model.values()),
        "recent_cases": [dict(row) for row in recent_cases],
        "recent": [dict(row) for row in recent],
    }


def existing_historical_article_enrichment_urls(urls: list[str]) -> set[str]:
    normalized = sorted({str(url or "").strip() for url in urls if str(url or "").strip()})
    if not normalized or not enabled():
        return set()
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT url
            FROM historical_article_enrichments
            WHERE url = ANY(%(urls)s)
              AND NOT (result ? 'processing_error')
            """,
            {"urls": normalized},
        ).fetchall()
    return {str(row["url"]) for row in rows}


def seed_historical_article_retries(pipeline_version: str) -> int:
    """Resume the one-time legacy migration in bounded, atomic pages."""
    if not enabled():
        return 0
    ensure_schema()
    while True:
        progress = _seed_historical_article_retry_page(pipeline_version)
        if progress["complete"]:
            return int(progress["seeded"])


def _seed_historical_article_retry_page(pipeline_version: str, *, limit: int = 1000) -> dict:
    name = f"legacy_article_retry_seed:v1:{pipeline_version}"
    with psycopg.connect(DATABASE_URL, connect_timeout=5,
                         options="-c lock_timeout=2000 -c statement_timeout=30000") as conn:
        conn.execute(
            "INSERT INTO worker_checkpoints (name, value) VALUES (%s, '{}'::jsonb) "
            "ON CONFLICT (name) DO NOTHING", (name,),
        )
        progress = dict(conn.execute(
            "SELECT value FROM worker_checkpoints WHERE name = %s FOR UPDATE", (name,),
        ).fetchone()[0])
        if progress.get("complete"):
            return progress
        if "through_url" not in progress:
            progress = {"through_url": conn.execute(
                "SELECT max(url) FROM historical_article_enrichments"
            ).fetchone()[0], "after_url": "", "seeded": 0, "scanned": 0}
        # Page ALL records before filtering failures, so healthy rows cannot cause
        # an unbounded scan. Retry insertion and cursor advancement commit together.
        row = conn.execute(
            """
            WITH page AS MATERIALIZED (
                SELECT url, original_title, source, published_ts, result, pipeline_version
                FROM historical_article_enrichments
                WHERE url > %(after_url)s AND url <= %(through_url)s
                ORDER BY url LIMIT %(limit)s
            ), inserted AS (
            INSERT INTO historical_article_retries (
                url, article, pipeline_version, attempts, last_error,
                next_attempt_at
            )
            SELECT
                url,
                jsonb_build_object(
                    'url', url,
                    'title', original_title,
                    'summary', '',
                    'source', source,
                    'published_ts', published_ts,
                    'provider', source,
                    'source_urls', jsonb_build_array(url),
                    'event_ids', '[]'::jsonb,
                    '_checkpoint_counted', false
                ),
                %(pipeline_version)s,
                0,
                COALESCE(result->>'processing_error', 'Legacy processing failure'),
                NOW()
            FROM page
            WHERE result ? 'processing_error'
              AND pipeline_version = %(pipeline_version)s
            ON CONFLICT (url) DO NOTHING
            RETURNING url
            )
            SELECT (SELECT count(*) FROM inserted), count(*), max(url) FROM page
            """,
            {**progress, "pipeline_version": pipeline_version,
             "limit": max(1, min(int(limit), 1000))},
        ).fetchone()
        progress.update(
            seeded=int(progress["seeded"]) + int(row[0]),
            scanned=int(progress["scanned"]) + int(row[1]),
            after_url=row[2] or progress["after_url"],
            complete=not row[1] or row[2] == progress["through_url"],
        )
        conn.execute(
            "UPDATE worker_checkpoints SET value = %s, updated_at = NOW() WHERE name = %s",
            (jsonb(progress), name),
        )
        return progress


def list_market_news_needing_verification(limit: int = 500) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT id, event_date::text AS date, event_type AS type, title, source,
                   url, symbols, confidence, extraction_source, details
            FROM calendar_events
            WHERE details->>'calendar_kind' = 'market_news'
              AND COALESCE(details->>'relevance_label', '') <> ''
              AND COALESCE(details->>'article_verification_status', '') = ''
            ORDER BY
                COALESCE((details->>'published_ts')::bigint, 0) DESC,
                event_date DESC,
                created_at DESC
            LIMIT %(limit)s
            """,
            {"limit": max(1, min(int(limit), 5000))},
        ).fetchall()
    return [dict(row) for row in rows]


def prune_calendar_events(extraction_source: str, retained_ids: list[str]) -> int:
    if not enabled():
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        if retained_ids:
            result = conn.execute(
                "DELETE FROM calendar_events WHERE extraction_source = %(source)s AND NOT (id = ANY(%(ids)s))",
                {"source": extraction_source, "ids": retained_ids},
            )
        else:
            result = conn.execute(
                "DELETE FROM calendar_events WHERE extraction_source = %(source)s",
                {"source": extraction_source},
            )
    return int(result.rowcount or 0)


def reclassify_news_earnings_events() -> int:
    if not enabled():
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        result = conn.execute(
            """
            UPDATE calendar_events
            SET event_type = 'Market note',
                details = details || '{"news_topic":"earnings"}'::jsonb,
                updated_at = NOW()
            WHERE extraction_source = 'news_rules'
              AND event_type = 'Earnings watch'
            """
        )
    return int(result.rowcount or 0)


def upsert_earnings_events(events: list[dict[str, Any]]) -> int:
    if not enabled() or not events:
        return 0
    ensure_schema()
    rows = [
        event for event in events
        if event.get("id") and event.get("source") and event.get("symbol") and event.get("event_date")
    ]
    if not rows:
        return 0
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO earnings_events (
                    id, source, symbol, company_name, event_date, event_name,
                    call_time, fiscal_quarter, fiscal_year, eps_estimate,
                    eps_actual, surprise_percent, market_cap_text,
                    market_cap_value, source_url, raw_payload, retrieved_at
                ) VALUES (
                    %(id)s, %(source)s, %(symbol)s, %(company_name)s,
                    %(event_date)s, %(event_name)s, %(call_time)s,
                    %(fiscal_quarter)s, %(fiscal_year)s, %(eps_estimate)s,
                    %(eps_actual)s, %(surprise_percent)s, %(market_cap_text)s,
                    %(market_cap_value)s, %(source_url)s, %(raw_payload)s, NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    event_name = EXCLUDED.event_name,
                    call_time = EXCLUDED.call_time,
                    fiscal_quarter = EXCLUDED.fiscal_quarter,
                    fiscal_year = EXCLUDED.fiscal_year,
                    eps_estimate = EXCLUDED.eps_estimate,
                    eps_actual = EXCLUDED.eps_actual,
                    surprise_percent = EXCLUDED.surprise_percent,
                    market_cap_text = EXCLUDED.market_cap_text,
                    market_cap_value = EXCLUDED.market_cap_value,
                    source_url = EXCLUDED.source_url,
                    raw_payload = EXCLUDED.raw_payload,
                    retrieved_at = NOW(),
                    updated_at = NOW()
                """,
                [{
                    **event,
                    "raw_payload": jsonb(event.get("raw_payload") or {}),
                } for event in rows],
            )
    return len(rows)


def earnings_event_counts() -> dict[str, int]:
    if not enabled():
        return {"total": 0, "reported": 0, "scheduled": 0}
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE eps_actual IS NOT NULL) AS reported,
                   COUNT(*) FILTER (WHERE eps_actual IS NULL) AS scheduled
            FROM earnings_events
            """
        ).fetchone()
    return {key: int(row.get(key) or 0) for key in ("total", "reported", "scheduled")}


def list_earnings_events(
    *,
    symbol: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    clauses = []
    params: dict[str, Any] = {
        "limit": max(1, min(int(limit), 5000)),
        "offset": max(0, int(offset)),
    }
    if symbol:
        clauses.append("symbol = %(symbol)s")
        params["symbol"] = str(symbol).upper()
    if start_date:
        clauses.append("event_date >= %(start_date)s")
        params["start_date"] = start_date
    if end_date:
        clauses.append("event_date <= %(end_date)s")
        params["end_date"] = end_date
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT id, source, symbol, company_name, event_date::text AS event_date,
                   event_name, call_time, fiscal_quarter, fiscal_year,
                   eps_estimate, eps_actual, surprise_percent, market_cap_text,
                   market_cap_value, source_url, raw_payload,
                   EXTRACT(EPOCH FROM retrieved_at)::bigint AS retrieved_ts
            FROM earnings_events
            {where}
            ORDER BY event_date DESC, symbol
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def update_calendar_event_relevance(event_id: str, relevance: dict[str, Any]) -> bool:
    if not enabled():
        return False
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            UPDATE calendar_events
            SET title = COALESCE(NULLIF(%(relevance)s->>'display_headline', ''), title),
                symbols = COALESCE(%(relevance)s->'related_tickers', '[]'::jsonb),
                details = details || %(relevance)s,
                updated_at = NOW()
            WHERE id = %(id)s
            RETURNING id, url, details
            """,
            {"id": event_id, "relevance": jsonb(relevance)},
        ).fetchone()
        if row:
            details = dict(row[2] or {})
            _insert_news_enrichment_version(
                conn,
                _news_enrichment_version(
                    record_type="calendar_event",
                    record_id=str(row[0]),
                    article_url=str(row[1]) if row[1] else None,
                    published_ts=int(details.get("published_ts") or 0),
                    pipeline_version=str(
                        details.get("pipeline_version")
                        or relevance.get("pipeline_version")
                        or "live"
                    ),
                    result=details,
                ),
            )
    return bool(row)


def normalize_calendar_news_ai_projection() -> int:
    if not enabled():
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SELECT pg_advisory_xact_lock(741852964)")
        checkpoint = "calendar_news_ai_projection:v1"
        if conn.execute(
            "SELECT 1 FROM worker_checkpoints WHERE name = %s AND value->>'complete' = 'true'",
            (checkpoint,),
        ).fetchone():
            return 0
        result = conn.execute(
            """
            UPDATE calendar_events
            SET title = CASE
                    WHEN COALESCE(details->>'display_headline', '') <> ''
                    THEN details->>'display_headline'
                    ELSE title
                END,
                symbols = COALESCE(details->'related_tickers', '[]'::jsonb),
                updated_at = NOW()
            WHERE details->>'calendar_kind' = 'market_news'
              AND (
                symbols IS DISTINCT FROM COALESCE(details->'related_tickers', '[]'::jsonb)
                OR (
                    COALESCE(details->>'display_headline', '') <> ''
                    AND title IS DISTINCT FROM details->>'display_headline'
                )
              )
            """
        )
        repaired = int(result.rowcount or 0)
        conn.execute(
            "INSERT INTO worker_checkpoints (name, value) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            (checkpoint, jsonb({"complete": True, "repaired": repaired})),
        )
    return repaired


def delete_calendar_event(event_id: str) -> bool:
    if not enabled():
        return False
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        result = conn.execute("DELETE FROM calendar_events WHERE id = %(id)s", {"id": event_id})
    return bool(result.rowcount)


def upsert_gdelt_mentions(rows: list[dict[str, Any]]) -> int:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    if not rows:
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO gdelt_event_mentions (
                    event_id, url, source, mention_ts, sentence_id, confidence, document_tone
                ) VALUES (
                    %(event_id)s, %(url)s, %(source)s, %(mention_ts)s,
                    %(sentence_id)s, %(confidence)s, %(document_tone)s
                )
                ON CONFLICT (event_id, url) DO UPDATE SET
                    source = EXCLUDED.source,
                    mention_ts = GREATEST(gdelt_event_mentions.mention_ts, EXCLUDED.mention_ts),
                    sentence_id = EXCLUDED.sentence_id,
                    confidence = GREATEST(gdelt_event_mentions.confidence, EXCLUDED.confidence),
                    document_tone = EXCLUDED.document_tone
                """,
                rows,
            )
    return len(rows)


def list_gdelt_mentions(event_id: str, limit: int = 20) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT event_id, url, source, mention_ts, sentence_id, confidence, document_tone
            FROM gdelt_event_mentions
            WHERE event_id = %(event_id)s
            ORDER BY confidence DESC NULLS LAST, mention_ts DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {"event_id": str(event_id), "limit": max(1, min(int(limit), 100))},
        ).fetchall()
    return [dict(row) for row in rows]


def list_gdelt_mentions_for_events(
    event_ids: list[str], *, limit: int = 2000
) -> list[dict[str, Any]]:
    """Load a bounded Mention set for event-time feature construction."""
    if not enabled():
        return []
    wanted = sorted({str(event_id) for event_id in event_ids if str(event_id).isdigit()})
    if not wanted:
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT event_id, url, source, mention_ts, sentence_id, confidence, document_tone
            FROM gdelt_event_mentions
            WHERE event_id = ANY(%(event_ids)s)
            ORDER BY mention_ts DESC NULLS LAST, confidence DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "event_ids": wanted[:100],
                "limit": max(1, min(int(limit), 5000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_news_quality_issues(issues: list[dict[str, Any]]) -> int:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    if not issues:
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO news_quality_issues (
                    issue_key, issue_type, severity, title, article_url, details
                ) VALUES (
                    %(issue_key)s, %(issue_type)s, %(severity)s, %(title)s,
                    %(article_url)s, %(details)s
                )
                ON CONFLICT (issue_key) DO UPDATE SET
                    severity = EXCLUDED.severity,
                    title = EXCLUDED.title,
                    article_url = EXCLUDED.article_url,
                    details = EXCLUDED.details,
                    last_seen = NOW()
                """,
                [{**issue, "details": jsonb(issue.get("details") or {})} for issue in issues],
            )
    return len(issues)


def save_news_quality_report(report: dict[str, Any]) -> int:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "INSERT INTO news_quality_runs (report) VALUES (%(report)s) RETURNING id",
            {"report": jsonb(report)},
        ).fetchone()
    return int(row[0])


def latest_news_quality_report() -> dict[str, Any] | None:
    if not enabled():
        return None
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT id, report, created_at FROM news_quality_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "created_at": row["created_at"].isoformat(), **dict(row["report"])}


def list_news_quality_issues(status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
    if not enabled():
        return []
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT issue_key, issue_type, severity, title, article_url, details,
                   status, review_note, first_seen, last_seen, reviewed_at
            FROM news_quality_issues
            WHERE status = %(status)s
            ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     last_seen DESC
            LIMIT %(limit)s
            """,
            {"status": status, "limit": max(1, min(int(limit), 100))},
        ).fetchall()
    return [
        {
            **dict(row),
            "details": dict(row["details"] or {}),
            "first_seen": row["first_seen"].isoformat(),
            "last_seen": row["last_seen"].isoformat(),
            "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
        }
        for row in rows
    ]


def review_news_quality_issue(issue_key: str, status: str, note: str = "") -> dict[str, Any] | None:
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    if status not in {"reviewed", "dismissed"}:
        raise ValueError("Invalid review status")
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            UPDATE news_quality_issues
            SET status = %(status)s, review_note = %(note)s, reviewed_at = NOW()
            WHERE issue_key = %(issue_key)s
            RETURNING issue_key, issue_type, severity, title, article_url, details,
                      status, review_note, reviewed_at
            """,
            {"issue_key": issue_key, "status": status, "note": note.strip() or None},
        ).fetchone()
    if not row:
        return None
    return {
        **dict(row),
        "details": dict(row["details"] or {}),
        "reviewed_at": row["reviewed_at"].isoformat(),
    }


def news_quality_issue_counts() -> dict[str, int]:
    if not enabled():
        return {}
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM news_quality_issues GROUP BY status"
        ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def gdelt_mentions_stats() -> dict[str, Any]:
    if not enabled():
        return {}
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS mentions, COUNT(DISTINCT event_id) AS events,
                   MIN(mention_ts) AS earliest, MAX(mention_ts) AS latest
            FROM gdelt_event_mentions
            """
        ).fetchone()
    return dict(row or {})


def auto_resolve_news_quality_issue_types(issue_types: list[str]) -> int:
    if not enabled() or not issue_types:
        return 0
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            UPDATE news_quality_issues
            SET status = 'auto_resolved', reviewed_at = NOW(),
                review_note = 'Moved to automated integrity findings'
            WHERE status = 'open' AND issue_type = ANY(%(issue_types)s)
            RETURNING issue_key
            """,
            {"issue_types": issue_types},
        ).fetchall()
    return len(row)


def _now_ms() -> int:
    return int(os.getenv("CALENDAR_STATE_TEST_NOW", "0")) or time.time_ns() // 1_000_000


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
