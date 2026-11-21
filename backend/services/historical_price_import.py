from __future__ import annotations

import hashlib
import os
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services import persistent_store


DATABASE_URL = os.getenv("DATABASE_URL")
IMPORT_VERSION = "schwab-price-history-v1"
BENCHMARK_SYMBOLS = tuple(
    symbol.strip().upper()
    for symbol in os.getenv("HISTORICAL_BENCHMARK_SYMBOLS", "SPY").split(",")
    if symbol.strip()
)
BENCHMARK_EXTENSION_MIN_SECONDS = max(
    300, int(os.getenv("HISTORICAL_BENCHMARK_EXTENSION_MIN_SECONDS", "3600"))
)

DDL = """
CREATE TABLE IF NOT EXISTS historical_price_imports (
    import_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    start_ts BIGINT NOT NULL,
    end_ts BIGINT NOT NULL,
    frequency_minutes INTEGER NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    candle_count INTEGER NOT NULL DEFAULT 0,
    stored_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    requested_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_historical_price_imports_status_created
    ON historical_price_imports (status, created_at);
"""


def ensure_schema() -> None:
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured")
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(741852966)")
        conn.execute(DDL)


def _import_key(symbol: str, start_ts: int, end_ts: int, frequency: int) -> str:
    value = f"{IMPORT_VERSION}|{symbol.upper()}|{start_ts}|{end_ts}|{frequency}"
    return hashlib.sha256(value.encode()).hexdigest()


def plan_candidate_imports(*, now_ts: int | None = None, frequency_minutes: int = 1) -> int:
    now = int(now_ts or time.time())
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT f.symbol, MIN(f.ts) AS first_ts, MAX(f.ts) AS last_ts
            FROM feature_store f
            WHERE f.source = 'historical_news_hype'
              AND NOT EXISTS (
                  SELECT 1 FROM historical_price_imports p
                  WHERE p.symbol = f.symbol
                    AND f.ts BETWEEN p.start_ts AND p.end_ts
              )
            GROUP BY f.symbol
            ORDER BY f.symbol
            """
        ).fetchall()
        planned = 0
        for row in rows:
            start_ts = max(1, int(row["first_ts"]) - 86_400)
            end_ts = min(now, int(row["last_ts"]) + 86_400)
            if end_ts <= start_ts:
                continue
            result = conn.execute(
                """
                INSERT INTO historical_price_imports (
                    import_key, symbol, start_ts, end_ts, frequency_minutes,
                    source, status
                ) VALUES (
                    %(key)s, %(symbol)s, %(start_ts)s, %(end_ts)s, %(frequency)s,
                    'schwab_price_history', 'pending'
                ) ON CONFLICT (import_key) DO NOTHING RETURNING import_key
                """,
                {"key": _import_key(row["symbol"], start_ts, end_ts, frequency_minutes),
                 "symbol": row["symbol"], "start_ts": start_ts, "end_ts": end_ts,
                 "frequency": int(frequency_minutes)},
            ).fetchone()
            planned += int(result is not None)
    return planned


def plan_benchmark_imports(*, now_ts: int | None = None, frequency_minutes: int = 1) -> int:
    if not BENCHMARK_SYMBOLS:
        return 0
    now = int(now_ts or time.time())
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        bounds = conn.execute(
            """SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts
               FROM feature_store WHERE source = 'historical_news_hype'"""
        ).fetchone()
        if not bounds or bounds["first_ts"] is None or bounds["last_ts"] is None:
            return 0
        desired_start = max(1, int(bounds["first_ts"]) - 86_400)
        desired_end = min(now, int(bounds["last_ts"]) + 86_400)
        if desired_end <= desired_start:
            return 0
        planned = 0
        for symbol in BENCHMARK_SYMBOLS:
            existing = conn.execute(
                """SELECT MIN(start_ts) AS first_ts, MAX(end_ts) AS last_ts
                   FROM historical_price_imports WHERE symbol = %(symbol)s""",
                {"symbol": symbol},
            ).fetchone()
            windows: list[tuple[int, int]] = []
            if not existing or existing["first_ts"] is None:
                windows.append((desired_start, desired_end))
            else:
                if int(existing["first_ts"]) - desired_start >= BENCHMARK_EXTENSION_MIN_SECONDS:
                    windows.append((desired_start, int(existing["first_ts"])))
                if desired_end - int(existing["last_ts"]) >= BENCHMARK_EXTENSION_MIN_SECONDS:
                    windows.append((int(existing["last_ts"]), desired_end))
            for start_ts, end_ts in windows:
                if end_ts <= start_ts:
                    continue
                result = conn.execute(
                    """
                    INSERT INTO historical_price_imports (
                        import_key, symbol, start_ts, end_ts, frequency_minutes,
                        source, status
                    ) VALUES (
                        %(key)s, %(symbol)s, %(start_ts)s, %(end_ts)s, %(frequency)s,
                        'schwab_price_history', 'pending'
                    ) ON CONFLICT (import_key) DO NOTHING RETURNING import_key
                    """,
                    {"key": _import_key(symbol, start_ts, end_ts, frequency_minutes),
                     "symbol": symbol, "start_ts": start_ts, "end_ts": end_ts,
                     "frequency": int(frequency_minutes)},
                ).fetchone()
                planned += int(result is not None)
    return planned


def next_import() -> dict[str, Any] | None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT * FROM historical_price_imports
            WHERE status IN ('pending', 'retry')
            ORDER BY created_at, symbol
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def recover_interrupted_imports() -> int:
    with psycopg.connect(DATABASE_URL) as conn:
        cursor = conn.execute(
            """UPDATE historical_price_imports
               SET status = 'retry',
                   last_error = 'worker_restarted_during_request',
                   updated_at = NOW()
               WHERE status = 'running'"""
        )
        return max(0, int(cursor.rowcount or 0))


def mark_started(import_key: str) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """UPDATE historical_price_imports SET status = 'running',
                      attempts = attempts + 1, requested_at = NOW(), updated_at = NOW()
               WHERE import_key = %(key)s""",
            {"key": import_key},
        )


def normalize_candles(task: dict[str, Any], payload: dict[str, Any], *, received_ts: int) -> list[dict[str, Any]]:
    rows = []
    for candle in payload.get("candles") or []:
        try:
            event_ts = int(candle.get("datetime") or 0) // 1000
            open_price = float(candle.get("open") or 0)
            high = float(candle.get("high") or 0)
            low = float(candle.get("low") or 0)
            close = float(candle.get("close") or 0)
            volume = max(float(candle.get("volume") or 0), 0)
        except (TypeError, ValueError):
            continue
        if (
            event_ts < int(task["start_ts"]) or event_ts > int(task["end_ts"])
            or min(open_price, high, low, close) <= 0
            or high < max(open_price, close) or low > min(open_price, close)
        ):
            continue
        raw = {
            "symbol": task["symbol"], "datetime": int(candle["datetime"]),
            "open": open_price, "high": high, "low": low, "close": close,
            "volume": volume, "frequency_minutes": int(task["frequency_minutes"]),
            "import_version": IMPORT_VERSION, "imported_ts": int(received_ts),
        }
        rows.append({
            "symbol": task["symbol"],
            "observation_type": "historical_minute_candle",
            "source": "schwab_price_history",
            "event_ts": event_ts,
            "received_ts": int(received_ts),
            "price": close, "open": open_price, "high": high, "low": low,
            "close": close, "volume": volume, "payload": raw,
        })
    return rows


def store_result(task: dict[str, Any], payload: dict[str, Any], *, received_ts: int) -> dict[str, int]:
    observations = normalize_candles(task, payload, received_ts=received_ts)
    stored = persistent_store.record_market_observations(observations, ensure=False)
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """UPDATE historical_price_imports SET status = 'complete',
                      candle_count = %(candles)s, stored_count = %(stored)s,
                      last_error = NULL, completed_at = NOW(), updated_at = NOW()
               WHERE import_key = %(key)s""",
            {"candles": len(observations), "stored": stored, "key": task["import_key"]},
        )
    return {"candles": len(observations), "stored": stored}


def mark_failed(task: dict[str, Any], error: str, *, maximum_attempts: int = 5) -> None:
    status = "retry" if int(task.get("attempts") or 0) + 1 < maximum_attempts else "failed"
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """UPDATE historical_price_imports SET status = %(status)s,
                      last_error = %(error)s, updated_at = NOW()
               WHERE import_key = %(key)s""",
            {"status": status, "error": str(error)[:1000], "key": task["import_key"]},
        )


def status(*, statement_timeout_ms: int = 5000, include_coverage: bool = True) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5,
                         options=f"-c statement_timeout={min(300000, max(1, int(statement_timeout_ms)))} -c lock_timeout=2000") as conn:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS count, COALESCE(SUM(candle_count), 0) AS candles
               FROM historical_price_imports GROUP BY status"""
        ).fetchall()
        coverage = conn.execute(
            """SELECT COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols,
                      MIN(event_ts) AS first_ts, MAX(event_ts) AS last_ts
               FROM market_observations
               WHERE observation_type = 'historical_minute_candle'
                 AND source = 'schwab_price_history'"""
        ).fetchone() if include_coverage else None
    return {
        "import_version": IMPORT_VERSION,
        "tasks": {row["status"]: int(row["count"]) for row in rows},
        "task_candles": sum(int(row["candles"]) for row in rows),
        "coverage": dict(coverage or {}),
        "coverage_status": "counted" if include_coverage else "not_requested",
        "observation_type": "historical_minute_candle",
        "source": "schwab_price_history",
        "retrospective": True,
        "live_received": False,
        "live_activation_eligible": False,
    }
