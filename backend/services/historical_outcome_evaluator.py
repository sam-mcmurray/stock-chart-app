from __future__ import annotations

from datetime import datetime, time as datetime_time, timezone
import hashlib
import json
import os
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.getenv("DATABASE_URL")
EVALUATOR_VERSION = "historical-news-outcome-v1"
DEFAULT_HORIZONS = (5, 15, 60, 390)
EASTERN = ZoneInfo("America/New_York")

DDL = """
CREATE TABLE IF NOT EXISTS historical_candidate_outcomes (
    outcome_key TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_ts BIGINT NOT NULL,
    evaluator_version TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    entry_event_ts BIGINT,
    entry_price DOUBLE PRECISION,
    latest_event_ts BIGINT,
    candle_count INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (candidate_id, evaluator_version)
);
CREATE INDEX IF NOT EXISTS idx_historical_outcomes_status_signal
    ON historical_candidate_outcomes (status, signal_ts DESC);
CREATE INDEX IF NOT EXISTS idx_historical_outcomes_symbol_signal
    ON historical_candidate_outcomes (symbol, signal_ts DESC);
"""


def ensure_schema() -> None:
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured")
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(741852967)")
        conn.execute(DDL)


def _outcome_key(candidate_id: str) -> str:
    return hashlib.sha256(f"{EVALUATOR_VERSION}|{candidate_id}".encode()).hexdigest()


def _is_regular_minute(event_ts: int) -> bool:
    value = datetime.fromtimestamp(int(event_ts), tz=timezone.utc).astimezone(EASTERN)
    return value.weekday() < 5 and datetime_time(9, 30) <= value.time() < datetime_time(16, 0)


def list_pending_candidates(*, limit: int = 100) -> list[dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, ts, payload FROM (
                SELECT DISTINCT ON (f.id) f.id, f.symbol, f.ts, f.payload
                FROM feature_store f
                JOIN historical_price_imports p
                  ON p.symbol = f.symbol
                 AND p.status = 'complete'
                 AND f.ts BETWEEN p.start_ts AND p.end_ts
                LEFT JOIN historical_candidate_outcomes o
                  ON o.candidate_id = f.id AND o.evaluator_version = %(version)s
                WHERE f.source = 'historical_news_hype'
                  AND o.candidate_id IS NULL
                ORDER BY f.id, p.completed_at DESC NULLS LAST
            ) AS ready
            ORDER BY ts DESC, id
            LIMIT %(limit)s
            """,
            {"version": EVALUATOR_VERSION, "limit": max(1, min(int(limit), 500))},
        ).fetchall()
    return [dict(row) for row in rows]


def load_candidate_candles(candidate: dict[str, Any], *, limit: int = 2000) -> list[dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (event_ts)
                       observation_key, event_ts, received_ts, open, high, low,
                       close, volume, payload
                FROM market_observations
                WHERE symbol = %(symbol)s
                  AND observation_type = 'historical_minute_candle'
                  AND source = 'schwab_price_history'
                  AND event_ts >= %(signal_ts)s
                ORDER BY event_ts, received_ts DESC, created_at DESC
            ) AS latest
            ORDER BY event_ts
            LIMIT %(limit)s
            """,
            {
                "symbol": str(candidate["symbol"]).upper(),
                "signal_ts": int(candidate["ts"]),
                "limit": max(1, min(int(limit), 5000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def evaluate_candidate(
    candidate: dict[str, Any],
    candles: list[dict[str, Any]],
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict[str, Any]:
    signal_ts = int(candidate["ts"])
    eligible = []
    for candle in candles:
        try:
            event_ts = int(candle.get("event_ts") or 0)
            values = {
                key: float(candle.get(key) or 0)
                for key in ("open", "high", "low", "close")
            }
            volume = max(0.0, float(candle.get("volume") or 0))
        except (TypeError, ValueError):
            continue
        if (
            event_ts <= signal_ts
            or not _is_regular_minute(event_ts)
            or min(values.values()) <= 0
            or values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
        ):
            continue
        eligible.append({**candle, **values, "event_ts": event_ts, "volume": volume})
    eligible.sort(key=lambda row: row["event_ts"])

    base = {
        "evaluator_version": EVALUATOR_VERSION,
        "candidate_id": candidate["id"],
        "symbol": str(candidate["symbol"]).upper(),
        "signal_ts": signal_ts,
        "source_observation_type": "historical_minute_candle",
        "source": "schwab_price_history",
        "retrospective": True,
        "live_received": False,
        "ranking_only": True,
        "live_activation_eligible": False,
        "entry_reference": "first_later_regular_session_candle_open",
    }
    if not eligible:
        return {**base, "status": "insufficient", "reason": "no_later_regular_candle",
                "entry_event_ts": None, "entry_price": None, "latest_event_ts": None,
                "candle_count": 0, "horizons": {}}

    entry = eligible[0]
    entry_price = float(entry["open"])
    results: dict[str, dict[str, Any]] = {}
    for raw_horizon in sorted(set(horizons)):
        horizon = int(raw_horizon)
        if horizon <= 0 or len(eligible) < horizon:
            continue
        window = eligible[:horizon]
        final = window[-1]
        results[str(horizon)] = {
            "bars": horizon,
            "end_event_ts": int(final["event_ts"]),
            "close": float(final["close"]),
            "return_pct": (float(final["close"]) / entry_price - 1) * 100,
            "mfe_pct": (max(float(row["high"]) for row in window) / entry_price - 1) * 100,
            "mae_pct": (min(float(row["low"]) for row in window) / entry_price - 1) * 100,
            "volume": sum(float(row["volume"]) for row in window),
            "ending_observation_key": final.get("observation_key"),
        }
    wanted = {str(int(value)) for value in horizons if int(value) > 0}
    status = "complete" if set(results) == wanted else ("partial" if results else "insufficient")
    reason = None if status == "complete" else "incomplete_forward_window"
    receipt_values = [int(row.get("received_ts") or 0) for row in eligible if row.get("received_ts")]
    return {
        **base,
        "status": status,
        "reason": reason,
        "entry_event_ts": int(entry["event_ts"]),
        "entry_price": entry_price,
        "entry_observation_key": entry.get("observation_key"),
        "latest_event_ts": int(eligible[-1]["event_ts"]),
        "candle_count": len(eligible),
        "import_received_ts_min": min(receipt_values) if receipt_values else None,
        "import_received_ts_max": max(receipt_values) if receipt_values else None,
        "horizons": results,
    }


def record_outcome(outcome: dict[str, Any]) -> bool:
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            INSERT INTO historical_candidate_outcomes (
                outcome_key, candidate_id, symbol, signal_ts, evaluator_version,
                status, reason, entry_event_ts, entry_price, latest_event_ts,
                candle_count, payload
            ) VALUES (
                %(outcome_key)s, %(candidate_id)s, %(symbol)s, %(signal_ts)s,
                %(evaluator_version)s, %(status)s, %(reason)s, %(entry_event_ts)s,
                %(entry_price)s, %(latest_event_ts)s, %(candle_count)s, %(payload)s
            ) ON CONFLICT (candidate_id, evaluator_version) DO NOTHING
            RETURNING outcome_key
            """,
            {
                **outcome,
                "outcome_key": _outcome_key(outcome["candidate_id"]),
                "payload": psycopg.types.json.Jsonb(outcome),
            },
        ).fetchone()
    return row is not None


def list_outcomes(
    *, symbol: str | None = None, status_filter: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    clauses = ["evaluator_version = %(version)s"]
    params: dict[str, Any] = {"version": EVALUATOR_VERSION}
    if symbol:
        clauses.append("symbol = %(symbol)s")
        params["symbol"] = str(symbol).upper()
    if status_filter:
        clauses.append("status = %(status)s")
        params["status"] = status_filter
    params["limit"] = max(1, min(int(limit), 500))
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""SELECT candidate_id, symbol, signal_ts, status, reason,
                       entry_event_ts, entry_price, latest_event_ts, candle_count,
                       payload, evaluated_at
                FROM historical_candidate_outcomes
                WHERE {' AND '.join(clauses)}
                ORDER BY signal_ts DESC, candidate_id
                LIMIT %(limit)s""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def status() -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        counts = conn.execute(
            """SELECT status, COUNT(*) AS count
               FROM historical_candidate_outcomes
               WHERE evaluator_version = %(version)s GROUP BY status""",
            {"version": EVALUATOR_VERSION},
        ).fetchall()
        pending = conn.execute(
            """SELECT COUNT(DISTINCT f.id) AS count
               FROM feature_store f
               JOIN historical_price_imports p
                 ON p.symbol = f.symbol AND p.status = 'complete'
                AND f.ts BETWEEN p.start_ts AND p.end_ts
               LEFT JOIN historical_candidate_outcomes o
                 ON o.candidate_id = f.id AND o.evaluator_version = %(version)s
               WHERE f.source = 'historical_news_hype' AND o.candidate_id IS NULL""",
            {"version": EVALUATOR_VERSION},
        ).fetchone()
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "outcomes": {row["status"]: int(row["count"]) for row in counts},
        "ready_pending": int(pending["count"] if pending else 0),
        "retrospective": True,
        "live_received": False,
        "ranking_only": True,
        "live_activation_eligible": False,
    }
