from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.getenv("DATABASE_URL")
_SCHEMA_READY = False

DDL = """
CREATE TABLE IF NOT EXISTS shadow_candidate_evaluations (
    candidate_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_ts BIGINT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    position_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    check_count INTEGER NOT NULL DEFAULT 1,
    first_evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE shadow_candidate_evaluations
    ADD COLUMN IF NOT EXISTS check_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE shadow_candidate_evaluations
    ADD COLUMN IF NOT EXISTS first_evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE INDEX IF NOT EXISTS idx_shadow_candidate_status_ts
    ON shadow_candidate_evaluations (status, signal_ts DESC);
CREATE TABLE IF NOT EXISTS shadow_opportunities (
    opportunity_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    first_signal_ts BIGINT NOT NULL,
    latest_signal_ts BIGINT NOT NULL,
    latest_candidate_id TEXT NOT NULL,
    last_evaluated_candidate_id TEXT,
    status TEXT NOT NULL DEFAULT 'watching',
    reason TEXT,
    position_id TEXT,
    check_count INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shadow_opportunities_status_updated
    ON shadow_opportunities (status, updated_at);
CREATE TABLE IF NOT EXISTS shadow_positions (
    position_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    opportunity_key TEXT,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    remaining_quantity INTEGER NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    hard_stop_price DOUBLE PRECISION NOT NULL,
    active_stop_price DOUBLE PRECISION NOT NULL,
    peak_price DOUBLE PRECISION NOT NULL,
    partial_taken BOOLEAN NOT NULL DEFAULT FALSE,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    strategy_version TEXT NOT NULL,
    signal_ts BIGINT NOT NULL,
    entry_event_ts BIGINT NOT NULL,
    entry_received_ts BIGINT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE shadow_positions ADD COLUMN IF NOT EXISTS opportunity_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_positions_opportunity
    ON shadow_positions (opportunity_key) WHERE opportunity_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_positions_one_active_symbol
    ON shadow_positions (symbol) WHERE state IN ('open', 'runner');
CREATE INDEX IF NOT EXISTS idx_shadow_positions_state_updated
    ON shadow_positions (state, updated_at DESC);
CREATE TABLE IF NOT EXISTS shadow_position_events (
    event_key TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES shadow_positions(position_id),
    event_type TEXT NOT NULL,
    event_ts BIGINT NOT NULL,
    price DOUBLE PRECISION,
    quantity INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shadow_position_events_position_ts
    ON shadow_position_events (position_id, event_ts, created_at);
"""


def _json(value: Any):
    return psycopg.types.json.Jsonb(value)


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured")
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(741852964)")
        conn.execute(DDL)
    _SCHEMA_READY = True


def upsert_shadow_opportunity(candidate: dict[str, Any]) -> dict[str, Any] | None:
    ensure_schema()
    payload = candidate.get("payload") or {}
    opportunity_key = str(payload.get("shadow_opportunity_key") or "").strip()
    if not opportunity_key:
        return None
    params = {
        "key": opportunity_key,
        "symbol": str(candidate["symbol"]).upper(),
        "signal_ts": int(candidate["ts"]),
        "candidate_id": candidate["id"],
        "payload": _json(payload),
    }
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            INSERT INTO shadow_opportunities (
                opportunity_key, symbol, first_signal_ts, latest_signal_ts,
                latest_candidate_id, payload
            ) VALUES (
                %(key)s, %(symbol)s, %(signal_ts)s, %(signal_ts)s,
                %(candidate_id)s, %(payload)s
            )
            ON CONFLICT (opportunity_key) DO UPDATE SET
                latest_signal_ts = EXCLUDED.latest_signal_ts,
                latest_candidate_id = EXCLUDED.latest_candidate_id,
                payload = EXCLUDED.payload,
                status = CASE
                    WHEN shadow_opportunities.status IN ('entered', 'skipped')
                    THEN shadow_opportunities.status ELSE 'watching' END,
                reason = CASE
                    WHEN shadow_opportunities.status IN ('entered', 'skipped')
                    THEN shadow_opportunities.reason ELSE NULL END,
                updated_at = NOW()
            WHERE EXCLUDED.latest_signal_ts >= shadow_opportunities.latest_signal_ts
            RETURNING *
            """,
            params,
        ).fetchone()
    return dict(row) if row else None


def expire_stale_opportunities(*, expire_before_ts: int) -> int:
    with psycopg.connect(DATABASE_URL) as conn:
        result = conn.execute(
            """UPDATE shadow_opportunities SET
                   status = 'expired', reason = 'candidate_expired', updated_at = NOW()
               WHERE status IN ('watching', 'monitoring')
                 AND latest_signal_ts < %(expire_before_ts)s""",
            {"expire_before_ts": int(expire_before_ts)},
        )
        return result.rowcount


def list_pending_opportunities(
    *, since_ts: int, expire_before_ts: int, limit: int = 100
) -> list[dict[str, Any]]:
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT latest_candidate_id AS id, opportunity_key, symbol,
                   latest_signal_ts AS ts, payload
            FROM shadow_opportunities
            WHERE latest_signal_ts >= %(since_ts)s
              AND status IN ('watching', 'monitoring')
              AND (
                    status = 'monitoring'
                    OR last_evaluated_candidate_id IS DISTINCT FROM latest_candidate_id
                    OR latest_signal_ts < %(expire_before_ts)s
              )
            ORDER BY (last_evaluated_candidate_id IS NULL) DESC,
                     updated_at,
                     opportunity_key
            LIMIT %(limit)s
            """,
            {
                "since_ts": int(since_ts),
                "expire_before_ts": int(expire_before_ts),
                "limit": max(1, min(int(limit), 500)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def record_opportunity_disposition(
    candidate: dict[str, Any], *, status: str, reason: str,
    position_id: str | None = None,
) -> bool:
    opportunity_key = str(candidate.get("opportunity_key") or "").strip()
    if not opportunity_key:
        return False
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            UPDATE shadow_opportunities SET
                status = %(status)s,
                reason = %(reason)s,
                position_id = %(position_id)s,
                last_evaluated_candidate_id = %(candidate_id)s,
                check_count = check_count + 1,
                updated_at = NOW()
            WHERE opportunity_key = %(key)s
              AND latest_candidate_id = %(candidate_id)s
            RETURNING opportunity_key
            """,
            {
                "status": status, "reason": reason, "position_id": position_id,
                "candidate_id": candidate["id"], "key": opportunity_key,
            },
        ).fetchone()
    return row is not None


def list_unprocessed_candidates(*, since_ts: int, limit: int = 100) -> list[dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT f.id, f.symbol, f.ts, f.price, f.payload
            FROM feature_store f
            LEFT JOIN shadow_candidate_evaluations e ON e.candidate_id = f.id
            WHERE f.source = 'scanner_shadow'
              AND f.ts >= %(since_ts)s
              AND (e.candidate_id IS NULL OR e.status = 'monitoring')
            ORDER BY (e.candidate_id IS NULL) DESC,
                     e.evaluated_at ASC NULLS FIRST,
                     f.ts,
                     f.id
            LIMIT %(limit)s
            """,
            {"since_ts": int(since_ts), "limit": max(1, min(int(limit), 500))},
        ).fetchall()
    return [dict(row) for row in rows]


def record_candidate_disposition(
    candidate: dict[str, Any], *, status: str, reason: str, position_id: str | None = None
) -> bool:
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            INSERT INTO shadow_candidate_evaluations (
                candidate_id, symbol, signal_ts, status, reason, position_id, payload
            ) VALUES (
                %(candidate_id)s, %(symbol)s, %(signal_ts)s, %(status)s,
                %(reason)s, %(position_id)s, %(payload)s
            ) ON CONFLICT (candidate_id) DO UPDATE SET
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                position_id = EXCLUDED.position_id,
                payload = EXCLUDED.payload,
                check_count = shadow_candidate_evaluations.check_count + 1,
                evaluated_at = NOW()
            WHERE shadow_candidate_evaluations.status = 'monitoring'
            RETURNING candidate_id
            """,
            {
                "candidate_id": candidate["id"],
                "symbol": candidate["symbol"],
                "signal_ts": int(candidate["ts"]),
                "status": status,
                "reason": reason,
                "position_id": position_id,
                "payload": _json(candidate.get("payload") or {}),
            },
        ).fetchone()
    return row is not None


def open_position(
    candidate: dict[str, Any],
    *,
    quantity: int,
    entry_price: float,
    hard_stop_price: float,
    event_ts: int,
    received_ts: int,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    opportunity_key = str(candidate.get("opportunity_key") or "").strip() or None
    position_identity = opportunity_key or candidate["id"]
    position_id = "shadow:" + hashlib.sha256(position_identity.encode()).hexdigest()[:32]
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        existing = conn.execute(
            """SELECT position_id FROM shadow_positions
               WHERE symbol = %(symbol)s AND state IN ('open', 'runner') FOR UPDATE""",
            {"symbol": candidate["symbol"]},
        ).fetchone()
        if existing:
            record = {
                "candidate_id": candidate["id"], "symbol": candidate["symbol"],
                "signal_ts": int(candidate["ts"]), "status": "skipped",
                "reason": "active_shadow_position", "position_id": existing["position_id"],
                "payload": _json(candidate.get("payload") or {}),
            }
            conn.execute(
                """INSERT INTO shadow_candidate_evaluations
                   (candidate_id, symbol, signal_ts, status, reason, position_id, payload)
                   VALUES (%(candidate_id)s, %(symbol)s, %(signal_ts)s, %(status)s,
                           %(reason)s, %(position_id)s, %(payload)s)
                   ON CONFLICT (candidate_id) DO UPDATE SET
                       status = EXCLUDED.status,
                       reason = EXCLUDED.reason,
                       position_id = EXCLUDED.position_id,
                       check_count = shadow_candidate_evaluations.check_count + 1,
                       evaluated_at = NOW()
                   WHERE shadow_candidate_evaluations.status = 'monitoring'""",
                record,
            )
            if opportunity_key:
                conn.execute(
                    """UPDATE shadow_opportunities SET
                           status = 'skipped', reason = 'active_shadow_position',
                           position_id = %(position_id)s,
                           last_evaluated_candidate_id = %(candidate_id)s,
                           check_count = check_count + 1, updated_at = NOW()
                       WHERE opportunity_key = %(opportunity_key)s
                         AND latest_candidate_id = %(candidate_id)s""",
                    {"position_id": existing["position_id"],
                     "candidate_id": candidate["id"],
                     "opportunity_key": opportunity_key},
                )
            return None
        inserted = conn.execute(
            """
            INSERT INTO shadow_candidate_evaluations
                (candidate_id, symbol, signal_ts, status, reason, position_id, payload)
            VALUES (%(candidate_id)s, %(symbol)s, %(signal_ts)s, 'entered',
                    'shadow_entry', %(position_id)s, %(candidate_payload)s)
            ON CONFLICT (candidate_id) DO UPDATE SET
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                position_id = EXCLUDED.position_id,
                check_count = shadow_candidate_evaluations.check_count + 1,
                evaluated_at = NOW()
            WHERE shadow_candidate_evaluations.status = 'monitoring'
            RETURNING candidate_id
            """,
            {
                "candidate_id": candidate["id"], "symbol": candidate["symbol"],
                "signal_ts": int(candidate["ts"]), "position_id": position_id,
                "candidate_payload": _json(candidate.get("payload") or {}),
            },
        ).fetchone()
        if not inserted:
            return None
        if opportunity_key:
            opportunity = conn.execute(
                """UPDATE shadow_opportunities SET
                       status = 'entered', reason = 'shadow_entry',
                       position_id = %(position_id)s,
                       last_evaluated_candidate_id = %(candidate_id)s,
                       check_count = check_count + 1, updated_at = NOW()
                   WHERE opportunity_key = %(opportunity_key)s
                     AND latest_candidate_id = %(candidate_id)s
                     AND status IN ('watching', 'monitoring')
                   RETURNING opportunity_key""",
                {"position_id": position_id, "candidate_id": candidate["id"],
                 "opportunity_key": opportunity_key},
            ).fetchone()
            if not opportunity:
                conn.rollback()
                return None
        position = conn.execute(
            """
            INSERT INTO shadow_positions (
                position_id, candidate_id, opportunity_key, symbol, state, quantity, remaining_quantity,
                entry_price, hard_stop_price, active_stop_price, peak_price,
                strategy_version, signal_ts, entry_event_ts, entry_received_ts, payload
            ) VALUES (
                %(position_id)s, %(candidate_id)s, %(opportunity_key)s, %(symbol)s, 'open', %(quantity)s,
                %(quantity)s, %(entry_price)s, %(hard_stop)s, %(hard_stop)s,
                %(entry_price)s, 'news-hype-shadow-v1', %(signal_ts)s, %(event_ts)s,
                %(received_ts)s, %(payload)s
            ) RETURNING *
            """,
            {
                "position_id": position_id, "candidate_id": candidate["id"],
                "opportunity_key": opportunity_key,
                "symbol": candidate["symbol"], "quantity": int(quantity),
                "entry_price": float(entry_price), "hard_stop": float(hard_stop_price),
                "signal_ts": int(candidate["ts"]), "event_ts": int(event_ts),
                "received_ts": int(received_ts), "payload": _json(payload),
            },
        ).fetchone()
        _insert_event(conn, position_id, "entry", event_ts, entry_price, quantity, payload)
    return dict(position) if position else None


def _insert_event(conn, position_id: str, event_type: str, event_ts: int,
                  price: float | None, quantity: int, payload: dict[str, Any]) -> None:
    identity = json.dumps(
        [position_id, event_type, int(event_ts), price, int(quantity), payload],
        sort_keys=True, separators=(",", ":"),
    )
    conn.execute(
        """INSERT INTO shadow_position_events
           (event_key, position_id, event_type, event_ts, price, quantity, payload)
           VALUES (%(key)s, %(position_id)s, %(event_type)s, %(event_ts)s,
                   %(price)s, %(quantity)s, %(payload)s)
           ON CONFLICT (event_key) DO NOTHING""",
        {"key": hashlib.sha256(identity.encode()).hexdigest(), "position_id": position_id,
         "event_type": event_type, "event_ts": int(event_ts), "price": price,
         "quantity": int(quantity), "payload": _json(payload)},
    )


def list_active_positions() -> list[dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT * FROM shadow_positions WHERE state IN ('open', 'runner') ORDER BY opened_at"
        ).fetchall()
    return [dict(row) for row in rows]


def apply_evaluation(
    position_id: str, *, evaluation: dict[str, Any], fill_price: float | None, event_ts: int
) -> dict[str, Any] | None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        position = conn.execute(
            "SELECT * FROM shadow_positions WHERE position_id = %(id)s FOR UPDATE",
            {"id": position_id},
        ).fetchone()
        if not position or position["state"] not in {"open", "runner"}:
            return None
        action = evaluation.get("action")
        remaining = int(position["remaining_quantity"])
        exit_quantity = min(max(int(evaluation.get("quantity") or 0), 0), remaining)
        new_remaining = remaining
        realized = float(position["realized_pnl"] or 0)
        state = position["state"]
        partial_taken = bool(position["partial_taken"])
        event_type = "risk_update"
        if action in {"take_partial", "exit_all"} and fill_price and exit_quantity > 0:
            new_remaining -= exit_quantity
            realized += exit_quantity * (float(fill_price) - float(position["entry_price"]))
            event_type = "partial_exit" if new_remaining > 0 else "exit"
            partial_taken = partial_taken or action == "take_partial"
            state = "runner" if new_remaining > 0 else "closed"
        updated = conn.execute(
            """
            UPDATE shadow_positions SET
                state = %(state)s, remaining_quantity = %(remaining)s,
                active_stop_price = GREATEST(active_stop_price, %(active_stop)s),
                peak_price = GREATEST(peak_price, %(peak)s), partial_taken = %(partial)s,
                realized_pnl = %(realized)s,
                closed_at = CASE WHEN %(state)s = 'closed' THEN NOW() ELSE closed_at END,
                updated_at = NOW()
            WHERE position_id = %(id)s RETURNING *
            """,
            {"state": state, "remaining": new_remaining,
             "active_stop": float(evaluation.get("active_stop") or position["active_stop_price"]),
             "peak": float(evaluation.get("peak_price") or position["peak_price"]),
             "partial": partial_taken, "realized": realized, "id": position_id},
        ).fetchone()
        # Quotes already provide the high-frequency price history. Persist a risk
        # event only when the actionable stop changes, not for every new peak.
        material = event_type != "risk_update" or (
            float(updated["active_stop_price"]) > float(position["active_stop_price"])
        )
        if material:
            _insert_event(
                conn, position_id, event_type, event_ts, fill_price, exit_quantity,
                {"evaluation": evaluation},
            )
    return dict(updated) if updated else None


def list_positions(*, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    where = "WHERE state = %(state)s" if state else ""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"SELECT * FROM shadow_positions {where} ORDER BY updated_at DESC LIMIT %(limit)s",
            {"state": state, "limit": max(1, min(int(limit), 500))},
        ).fetchall()
    return [dict(row) for row in rows]


def list_events(*, position_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    where = "WHERE position_id = %(position_id)s" if position_id else ""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""SELECT event_key, position_id, event_type, event_ts, price, quantity,
                       payload, created_at
                FROM shadow_position_events {where}
                ORDER BY event_ts DESC, created_at DESC LIMIT %(limit)s""",
            {"position_id": position_id, "limit": max(1, min(int(limit), 1000))},
        ).fetchall()
    return [dict(row) for row in rows]


def list_candidate_evaluations(
    *, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    where = "WHERE status = %(status)s" if status else ""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""SELECT candidate_id, symbol, signal_ts, status, reason, position_id,
                       check_count, first_evaluated_at, evaluated_at
                FROM shadow_candidate_evaluations {where}
                ORDER BY (status = 'monitoring') DESC, evaluated_at DESC
                LIMIT %(limit)s""",
            {"status": status, "limit": max(1, min(int(limit), 500))},
        ).fetchall()
    return [dict(row) for row in rows]


def list_opportunities(*, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    where = "WHERE status = %(status)s" if status else ""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""SELECT opportunity_key AS candidate_id, opportunity_key,
                       symbol, latest_signal_ts AS signal_ts, status, reason,
                       position_id, check_count, created_at AS first_evaluated_at,
                       updated_at AS evaluated_at
                FROM shadow_opportunities {where}
                ORDER BY (status = 'monitoring') DESC, updated_at DESC
                LIMIT %(limit)s""",
            {"status": status, "limit": max(1, min(int(limit), 500))},
        ).fetchall()
    return [dict(row) for row in rows]


def summary() -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS count, COALESCE(SUM(realized_pnl), 0) AS pnl FROM shadow_positions GROUP BY state"
        ).fetchall()
        candidates = conn.execute(
            "SELECT status, COUNT(*) AS count FROM shadow_candidate_evaluations GROUP BY status"
        ).fetchall()
        checks = conn.execute(
            "SELECT COALESCE(SUM(check_count), 0) AS count FROM shadow_candidate_evaluations"
        ).fetchone()
        opportunities = conn.execute(
            "SELECT status, COUNT(*) AS count FROM shadow_opportunities GROUP BY status"
        ).fetchall()
        opportunity_checks = conn.execute(
            "SELECT COALESCE(SUM(check_count), 0) AS count FROM shadow_opportunities"
        ).fetchone()
    return {
        "positions": {row["state"]: int(row["count"]) for row in rows},
        "realized_pnl": round(sum(float(row["pnl"]) for row in rows), 6),
        "candidates": {row["status"]: int(row["count"]) for row in candidates},
        "candidate_checks": int(checks["count"]),
        "opportunities": {row["status"]: int(row["count"]) for row in opportunities},
        "opportunity_checks": int(opportunity_checks["count"]),
        "execution_mode": "shadow",
        "broker_calls": 0,
        "live_activation_eligible": False,
    }
