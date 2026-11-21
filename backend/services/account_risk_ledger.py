from __future__ import annotations

from datetime import datetime
import math
import os
from typing import Any
from zoneinfo import ZoneInfo

from services import persistent_store
from services.protective_stop import normalize_equity_stop_price


PROTECTED_STATUSES = frozenset({"submitted", "partially_filled"})


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def summarize_account_risk(
    *,
    positions: list[dict[str, Any]],
    protective_intents: dict[str, dict[str, Any]],
    sell_fills: list[dict[str, Any]],
    unsettled_intents: list[dict[str, Any]] | None = None,
    require_broker_protection: bool = True,
) -> dict[str, Any]:
    complete = True
    reasons: list[str] = []
    aggregate_open_risk = 0.0
    positions_at_risk = 0
    position_by_id = {str(row.get("position_id")): row for row in positions}

    for intent in unsettled_intents or []:
        key = str(intent.get("idempotency_key") or "unknown")
        complete = False
        reasons.append(f"unsettled_order:{key}")

    for position_id, position in position_by_id.items():
        state = str(position.get("state") or "").lower()
        remaining = max(_number(position.get("remaining_quantity")), 0.0)
        if state in {"closed", "blocked"}:
            continue
        if state == "entry_pending":
            complete = False
            reasons.append(f"entry_pending:{position_id}")
        if remaining <= 0:
            if state != "entry_pending":
                complete = False
                reasons.append(f"invalid_position_quantity:{position_id}")
            continue
        entry = _number(position.get("average_entry_price"))
        hard_stop = _number(position.get("hard_stop_price"))
        stop = _number(position.get("active_stop_price"))
        intent = protective_intents.get(position_id) or {}
        payload = intent.get("order_payload") or {}
        intended_stop = _number(payload.get("stopPrice"))
        quantity_matches = int(_number(intent.get("quantity"))) == math.floor(remaining)
        stop_matches = False
        if stop > 0 and intended_stop > 0:
            stop_matches = normalize_equity_stop_price(stop) == normalize_equity_stop_price(intended_stop)
        if require_broker_protection:
            protected = (
                str(intent.get("status") or "").lower() in PROTECTED_STATUSES
                and bool(intent.get("broker_order_id"))
                and quantity_matches
                and stop_matches
            )
        else:
            protected = hard_stop > 0 and hard_stop < entry and stop >= hard_stop
        if entry <= 0 or stop <= 0 or not protected:
            complete = False
            reasons.append(f"unverified_protection:{position_id}")
            continue
        positions_at_risk += 1
        aggregate_open_risk += remaining * max(entry - stop, 0.0)

    daily_realized_pnl = 0.0
    for fill in sell_fills:
        position = position_by_id.get(str(fill.get("position_id")))
        if not position:
            complete = False
            reasons.append("sell_fill_without_position")
            continue
        quantity = _number(fill.get("quantity"))
        price = _number(fill.get("price"))
        entry = _number(position.get("average_entry_price"))
        if quantity <= 0 or price <= 0 or entry <= 0:
            complete = False
            reasons.append(f"invalid_sell_fill:{fill.get('fill_key') or 'unknown'}")
            continue
        daily_realized_pnl += quantity * (price - entry)

    return {
        "daily_realized_pnl": round(daily_realized_pnl, 8),
        "aggregate_open_risk": round(aggregate_open_risk, 8),
        "risk_state_complete": complete,
        "positions_at_risk": positions_at_risk,
        "incomplete_reasons": sorted(set(reasons)),
    }


def get_account_risk_summary(*, account_id: str, now: datetime | None = None) -> dict[str, Any]:
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    account = str(account_id or "").strip()
    if not account:
        raise ValueError("account_id is required")
    timezone_name = os.getenv("AGENT_RISK_TIMEZONE", "America/New_York")
    zone = ZoneInfo(timezone_name)
    session_date = (now or datetime.now(zone)).astimezone(zone).date()
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        positions = [dict(row) for row in conn.execute(
            "SELECT * FROM trade_positions WHERE account_id = %(account_id)s",
            {"account_id": account},
        ).fetchall()]
        intents = [dict(row) for row in conn.execute(
            """
            SELECT DISTINCT ON (position_id) * FROM trade_order_intents
            WHERE account_id = %(account_id)s AND intent_role = 'protective_stop'
              AND position_id IS NOT NULL
            ORDER BY position_id, updated_at DESC
            """,
            {"account_id": account},
        ).fetchall()]
        sell_fills = [dict(row) for row in conn.execute(
            """
            SELECT f.* FROM trade_fills f
            JOIN trade_order_intents i ON i.idempotency_key = f.idempotency_key
            WHERE i.account_id = %(account_id)s
              AND UPPER(COALESCE(f.instruction, '')) IN ('SELL', 'SELL_SHORT')
              AND (f.executed_at AT TIME ZONE %(timezone)s)::date = %(session_date)s
            """,
            {
                "account_id": account,
                "timezone": timezone_name,
                "session_date": session_date,
            },
        ).fetchall()]
        unsettled_intents = [dict(row) for row in conn.execute(
            """
            SELECT idempotency_key, status FROM trade_order_intents
            WHERE account_id = %(account_id)s
              AND intent_role <> 'protective_stop'
              AND status IN (
                  'reserved', 'submitting', 'unknown', 'submitted',
                  'partially_filled', 'cancel_pending'
              )
            """,
            {"account_id": account},
        ).fetchall()]
    return summarize_account_risk(
        positions=positions,
        protective_intents={str(row["position_id"]): row for row in intents},
        sell_fills=sell_fills,
        unsettled_intents=unsettled_intents,
        require_broker_protection=os.getenv(
            "AGENT_BROKER_PROTECTIVE_STOPS_ENABLED", "false"
        ).lower() == "true",
    )
