from __future__ import annotations

from dataclasses import dataclass
import math
import re
import hashlib
from typing import Any

from services import persistent_store


IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "rejected", "failed"})
ACTIVE_ORDER_STATUSES = frozenset({
    "reserved", "submitting", "unknown", "submitted", "partially_filled", "cancel_pending"
})
INTENT_ROLES = frozenset({"discretionary", "entry", "exit", "protective_stop"})
ORDER_STATUS_RANK = {
    "reserved": 0,
    "unknown": 0,
    "submitting": 1,
    "submitted": 2,
    "cancel_pending": 3,
    "partially_filled": 4,
    "canceled": 5,
    "rejected": 5,
    "failed": 5,
    "filled": 6,
}


class IdempotencyConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderIntentReservation:
    intent: dict[str, Any]
    created: bool


def merge_order_snapshot(
    intent: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Merge an authoritative snapshot without allowing reordered data to regress state."""
    current_status = str(intent.get("status") or "unknown").lower()
    incoming_status = str(snapshot.get("status") or "unknown").lower()
    current_rank = ORDER_STATUS_RANK.get(current_status, 0)
    incoming_rank = ORDER_STATUS_RANK.get(incoming_status, 0)
    status_advances = (
        incoming_status != "unknown"
        and (incoming_rank > current_rank or incoming_status == current_status)
    )
    merged_status = incoming_status if status_advances else current_status

    def nonnegative_number(value: Any) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) and number >= 0 else 0.0

    current_filled = nonnegative_number(intent.get("filled_quantity"))
    incoming_filled = nonnegative_number(snapshot.get("filled_quantity"))
    merged_filled = max(current_filled, incoming_filled)
    current_average = nonnegative_number(intent.get("average_fill_price"))
    incoming_average = nonnegative_number(snapshot.get("average_fill_price"))
    average_advances = incoming_filled > current_filled or (
        incoming_filled == current_filled
        and incoming_filled > 0
        and status_advances
    )
    merged_average = (
        incoming_average
        if average_advances and incoming_average > 0
        else current_average
    )
    quantity = nonnegative_number(intent.get("quantity"))
    merged_remaining = max(quantity - merged_filled, 0.0)
    payload_advances = status_advances or incoming_filled > current_filled
    return {
        "status": merged_status,
        "filled_quantity": merged_filled,
        "remaining_quantity": merged_remaining,
        "average_fill_price": merged_average,
        "raw": (
            snapshot.get("raw")
            if payload_advances
            else intent.get("last_broker_payload") or {}
        ),
    }


def reduce_long_position_fills(
    fills: list[dict[str, Any]],
    *,
    intended_quantity: int,
    entry_terminal_without_fill: bool = False,
) -> dict[str, Any]:
    """Rebuild a long position from immutable BUY/SELL executions."""
    buys = []
    sells = []
    for fill in fills:
        try:
            quantity = float(fill.get("quantity") or 0)
            price = float(fill.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(quantity) or not math.isfinite(price) or quantity <= 0 or price <= 0:
            continue
        instruction = str(fill.get("instruction") or "").upper()
        if instruction in {"BUY", "BUY_TO_COVER"}:
            buys.append((quantity, price))
        elif instruction in {"SELL", "SELL_SHORT"}:
            sells.append((quantity, price))

    bought_quantity = sum(quantity for quantity, _ in buys)
    sold_quantity = min(sum(quantity for quantity, _ in sells), bought_quantity)
    remaining_quantity = max(bought_quantity - sold_quantity, 0.0)
    average_entry_price = (
        sum(quantity * price for quantity, price in buys) / bought_quantity
        if bought_quantity > 0 else 0.0
    )
    realized_pnl = 0.0
    quantity_left_to_match = sold_quantity
    for quantity, price in sells:
        matched = min(quantity, quantity_left_to_match)
        realized_pnl += matched * (price - average_entry_price)
        quantity_left_to_match -= matched
        if quantity_left_to_match <= 0:
            break

    partial_taken = sold_quantity > 0 and remaining_quantity > 0
    if bought_quantity <= 0:
        state = "blocked" if entry_terminal_without_fill else "entry_pending"
    elif remaining_quantity <= 0:
        state = "closed"
    elif partial_taken:
        state = "partial_taken"
    elif bought_quantity < max(int(intended_quantity), 0):
        state = "entry_pending"
    else:
        state = "open"
    return {
        "state": state,
        "filled_quantity": bought_quantity,
        "remaining_quantity": remaining_quantity,
        "average_entry_price": average_entry_price,
        "partial_taken": partial_taken,
        "realized_pnl": realized_pnl,
    }


def validate_idempotency_key(value: str | None) -> str:
    key = str(value or "").strip()
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValueError(
            "Idempotency-Key must be 8-128 characters using letters, numbers, '.', '_', ':', or '-'"
        )
    return key


def _normalize_order(
    *,
    account_id: str,
    symbol: str,
    side: str,
    quantity: int,
    order_type: str,
) -> dict[str, Any]:
    normalized = {
        "account_id": str(account_id or "").strip(),
        "symbol": str(symbol or "").strip().upper(),
        "side": str(side or "").strip().upper(),
        "quantity": int(quantity or 0),
        "order_type": str(order_type or "").strip().upper(),
    }
    if not normalized["account_id"]:
        raise ValueError("account_id is required")
    if not re.fullmatch(r"[A-Z0-9.$/-]{1,32}", normalized["symbol"]):
        raise ValueError("invalid symbol")
    if normalized["side"] not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if normalized["quantity"] < 1:
        raise ValueError("quantity must be at least one")
    if normalized["order_type"] not in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}:
        raise ValueError("unsupported order type")
    return normalized


def _assert_same_intent(existing: dict[str, Any], expected: dict[str, Any]) -> None:
    compared = ("account_id", "symbol", "side", "quantity", "order_type")
    mismatched = [key for key in compared if existing.get(key) != expected.get(key)]
    if mismatched:
        raise IdempotencyConflict(
            f"Idempotency-Key is already reserved for a different order ({', '.join(mismatched)})"
        )


def reserve_order_intent(
    *,
    idempotency_key: str,
    account_id: str,
    symbol: str,
    side: str,
    quantity: int,
    order_type: str,
    order_payload: dict[str, Any],
    position_id: str | None = None,
    intent_role: str = "discretionary",
    replaces_idempotency_key: str | None = None,
) -> OrderIntentReservation:
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    key = validate_idempotency_key(idempotency_key)
    normalized = _normalize_order(
        account_id=account_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
    )
    persistent_store.ensure_schema()
    params = {
        "idempotency_key": key,
        "position_id": position_id,
        "intent_role": str(intent_role or "").strip().lower(),
        "replaces_idempotency_key": replaces_idempotency_key,
        **normalized,
        "order_payload": persistent_store.jsonb(order_payload),
    }
    if params["intent_role"] not in INTENT_ROLES:
        raise ValueError("invalid order intent role")
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        row = conn.execute(
            """
            INSERT INTO trade_order_intents (
                idempotency_key, position_id, account_id, symbol, side,
                quantity, order_type, intent_role, replaces_idempotency_key, order_payload
            ) VALUES (
                %(idempotency_key)s, %(position_id)s, %(account_id)s, %(symbol)s, %(side)s,
                %(quantity)s, %(order_type)s, %(intent_role)s, %(replaces_idempotency_key)s,
                %(order_payload)s
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            params,
        ).fetchone()
        created = row is not None
        if row is None:
            row = conn.execute(
                "SELECT * FROM trade_order_intents WHERE idempotency_key = %(idempotency_key)s",
                {"idempotency_key": key},
            ).fetchone()
    if not row:
        raise RuntimeError("Unable to reserve or retrieve trade order intent")
    intent = dict(row)
    _assert_same_intent(intent, normalized)
    if intent.get("intent_role", "discretionary") != params["intent_role"]:
        raise IdempotencyConflict("Idempotency-Key is already reserved for a different intent role")
    if position_id is not None and intent.get("position_id") != position_id:
        raise IdempotencyConflict("Idempotency-Key is already reserved for a different position")
    return OrderIntentReservation(intent=intent, created=created)


def list_positions_for_protection(*, limit: int = 200) -> list[dict[str, Any]]:
    """Return positions needing protection plus their newest active stop intent."""
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    bounded_limit = max(1, min(int(limit), 1000))
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        positions = conn.execute(
            """
            SELECT * FROM trade_positions p
            WHERE p.state NOT IN ('closed', 'blocked')
               OR EXISTS (
                    SELECT 1 FROM trade_order_intents i
                    WHERE i.position_id = p.position_id
                      AND i.intent_role = 'protective_stop'
                      AND i.status IN ('reserved', 'unknown', 'submitted', 'partially_filled', 'cancel_pending')
               )
            ORDER BY p.updated_at
            LIMIT %(limit)s
            """,
            {"limit": bounded_limit},
        ).fetchall()
        output: list[dict[str, Any]] = []
        for source in positions:
            position = dict(source)
            protective_intent = conn.execute(
                """
                SELECT * FROM trade_order_intents
                WHERE position_id = %(position_id)s
                  AND intent_role = 'protective_stop'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                {"position_id": position["position_id"]},
            ).fetchone()
            exit_intent = conn.execute(
                """
                SELECT * FROM trade_order_intents
                WHERE position_id = %(position_id)s
                  AND intent_role = 'exit'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                {"position_id": position["position_id"]},
            ).fetchone()
            output.append({
                "position": position,
                "protective_intent": dict(protective_intent) if protective_intent else None,
                "exit_intent": dict(exit_intent) if exit_intent else None,
            })
    return output


def list_reconcilable_order_intents(*, limit: int = 100) -> list[dict[str, Any]]:
    """Return broker orders that are active or missing their terminal REST snapshot."""
    if not persistent_store.enabled():
        return []
    bounded_limit = max(1, min(int(limit), 500))
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        rows = conn.execute(
            """
            SELECT * FROM trade_order_intents
            WHERE broker_order_id IS NOT NULL
              AND (
                  status IN ('unknown', 'submitted', 'partially_filled', 'cancel_pending')
                  OR (
                      status IN ('filled', 'canceled', 'rejected', 'failed')
                      AND last_broker_payload IS NULL
                  )
              )
            ORDER BY updated_at
            LIMIT %(limit)s
            """,
            {"limit": bounded_limit},
        ).fetchall()
    return [dict(row) for row in rows]


def list_unidentified_order_intents(*, limit: int = 100) -> list[dict[str, Any]]:
    """Return ambiguous submissions whose broker order ID was never persisted."""
    if not persistent_store.enabled():
        return []
    bounded_limit = max(1, min(int(limit), 500))
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        rows = conn.execute(
            """
            SELECT * FROM trade_order_intents
            WHERE broker_order_id IS NULL
              AND status IN ('submitting', 'unknown')
            ORDER BY updated_at
            LIMIT %(limit)s
            """,
            {"limit": bounded_limit},
        ).fetchall()
    return [dict(row) for row in rows]


def attach_broker_order_identity(
    idempotency_key: str, *, broker_order_id: int
) -> dict[str, Any] | None:
    """Attach a recovered broker ID unless either side was already claimed."""
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    key = validate_idempotency_key(idempotency_key)
    order_id = int(broker_order_id)
    if order_id <= 0:
        raise ValueError("broker_order_id is required")
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        row = conn.execute(
            """
            UPDATE trade_order_intents AS target
            SET broker_order_id = %(broker_order_id)s, updated_at = NOW()
            WHERE target.idempotency_key = %(idempotency_key)s
              AND target.broker_order_id IS NULL
              AND target.status IN ('submitting', 'unknown')
              AND NOT EXISTS (
                  SELECT 1 FROM trade_order_intents AS claimed
                  WHERE claimed.broker_order_id = %(broker_order_id)s
                    AND claimed.idempotency_key <> target.idempotency_key
              )
            RETURNING *
            """,
            {"idempotency_key": key, "broker_order_id": order_id},
        ).fetchone()
    return dict(row) if row else None


def list_active_position_symbols(*, limit: int = 200) -> list[str]:
    if not persistent_store.enabled():
        return []
    bounded_limit = max(1, min(int(limit), 1000))
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(persistent_store.DATABASE_URL) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT symbol FROM trade_positions
            WHERE state NOT IN ('closed', 'blocked')
            ORDER BY symbol
            LIMIT %(limit)s
            """,
            {"limit": bounded_limit},
        ).fetchall()
    return [str(row[0]).upper() for row in rows if row and row[0]]


def update_position_risk_state(
    *,
    position_id: str,
    expected_remaining_quantity: float,
    peak_price: float,
    active_stop_price: float,
    decision: dict[str, Any],
    event_ts: int,
) -> dict[str, Any]:
    """Ratchet a position's peak/stop forward without accepting a stale quantity view."""
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    position_key = str(position_id or "").strip()
    expected = float(expected_remaining_quantity)
    proposed_peak = float(peak_price)
    proposed_stop = float(active_stop_price)
    if not position_key:
        raise ValueError("position_id is required")
    if not all(math.isfinite(value) for value in (expected, proposed_peak, proposed_stop)):
        raise ValueError("risk state values must be finite")
    if expected < 0 or proposed_peak <= 0 or proposed_stop <= 0:
        raise ValueError("risk state values are invalid")
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        source = conn.execute(
            "SELECT * FROM trade_positions WHERE position_id = %(position_id)s FOR UPDATE",
            {"position_id": position_key},
        ).fetchone()
        if not source:
            raise KeyError(f"Unknown position: {position_key}")
        position = dict(source)
        current_remaining = float(position.get("remaining_quantity") or 0)
        if not math.isclose(current_remaining, expected, abs_tol=1e-9):
            return {
                "updated": False,
                "reason": "position_quantity_changed",
                "position": position,
            }
        hard_stop = float(position.get("hard_stop_price") or 0)
        entry = float(position.get("average_entry_price") or 0)
        current_peak = float(position.get("peak_price") or entry or 0)
        current_stop = float(position.get("active_stop_price") or hard_stop or 0)
        next_peak = max(current_peak, proposed_peak, entry)
        next_stop = max(current_stop, proposed_stop, hard_stop)
        if hard_stop <= 0 or entry <= hard_stop or next_stop <= 0:
            return {
                "updated": False,
                "reason": "invalid_position_risk_state",
                "position": position,
            }
        changed = not (
            math.isclose(next_peak, current_peak, abs_tol=1e-9)
            and math.isclose(next_stop, current_stop, abs_tol=1e-9)
        )
        if changed:
            position = dict(conn.execute(
                """
                UPDATE trade_positions
                SET peak_price = %(peak_price)s,
                    active_stop_price = %(active_stop_price)s,
                    updated_at = NOW()
                WHERE position_id = %(position_id)s
                RETURNING *
                """,
                {
                    "position_id": position_key,
                    "peak_price": next_peak,
                    "active_stop_price": next_stop,
                },
            ).fetchone())
        action = str(decision.get("action") or "hold")
        reason = str(decision.get("reason") or "")
        if changed or action != "hold":
            payload = {
                **decision,
                "previous_peak_price": current_peak,
                "previous_active_stop_price": current_stop,
                "peak_price": next_peak,
                "active_stop_price": next_stop,
            }
            event_key = "risk:" + hashlib.sha256(
                (
                    f"{position_key}|{next_peak:.8f}|{next_stop:.8f}|"
                    f"{action}|{reason}"
                ).encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO trade_position_events (
                    event_key, position_id, event_type, event_ts, payload
                ) VALUES (
                    %(event_key)s, %(position_id)s, 'risk_decision', %(event_ts)s, %(payload)s
                )
                ON CONFLICT (event_key) DO NOTHING
                """,
                {
                    "event_key": event_key,
                    "position_id": position_key,
                    "event_ts": int(event_ts),
                    "payload": persistent_store.jsonb(payload),
                },
            )
        return {"updated": changed, "reason": None, "position": position}


def ensure_entry_position(
    *,
    idempotency_key: str,
    account_id: str,
    symbol: str,
    intended_quantity: int,
    hard_stop_price: float,
    strategy_version: str,
) -> str:
    """Attach one deterministic pending position to a reserved BUY intent."""
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    key = validate_idempotency_key(idempotency_key)
    stop = float(hard_stop_price)
    if not math.isfinite(stop) or stop <= 0:
        raise ValueError("hard_stop_price must be positive")
    if int(intended_quantity) < 1:
        raise ValueError("intended_quantity must be at least one")
    position_id = "pos:" + hashlib.sha256(
        f"{account_id}|{symbol.upper()}|{key}".encode("utf-8")
    ).hexdigest()
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        intent = conn.execute(
            "SELECT * FROM trade_order_intents WHERE idempotency_key = %(key)s FOR UPDATE",
            {"key": key},
        ).fetchone()
        if not intent:
            raise KeyError(f"Unknown order intent: {key}")
        if intent.get("position_id"):
            return str(intent["position_id"])
        active = conn.execute(
            """
            SELECT position_id FROM trade_positions
            WHERE account_id = %(account_id)s AND symbol = %(symbol)s
              AND state NOT IN ('closed', 'blocked')
            FOR UPDATE
            """,
            {"account_id": account_id, "symbol": symbol.upper()},
        ).fetchone()
        if active:
            raise IdempotencyConflict("An active position already exists for this account and symbol")
        conn.execute(
            """
            INSERT INTO trade_positions (
                position_id, account_id, symbol, state, intended_quantity,
                hard_stop_price, active_stop_price, strategy_version
            ) VALUES (
                %(position_id)s, %(account_id)s, %(symbol)s, 'entry_pending',
                %(intended_quantity)s, %(hard_stop_price)s, %(hard_stop_price)s,
                %(strategy_version)s
            )
            """,
            {
                "position_id": position_id,
                "account_id": account_id,
                "symbol": symbol.upper(),
                "intended_quantity": int(intended_quantity),
                "hard_stop_price": stop,
                "strategy_version": strategy_version,
            },
        )
        conn.execute(
            "UPDATE trade_order_intents SET position_id = %(position_id)s WHERE idempotency_key = %(key)s",
            {"position_id": position_id, "key": key},
        )
    return position_id


def update_order_intent(
    idempotency_key: str,
    *,
    status: str,
    broker_order_id: int | None = None,
    broker_location: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    key = validate_idempotency_key(idempotency_key)
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {
        "reserved", "submitting", "submitted", "partially_filled", "filled", "cancel_pending",
        "canceled", "rejected", "failed", "unknown",
    }:
        raise ValueError("invalid order intent status")
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        row = conn.execute(
            """
            UPDATE trade_order_intents
            SET status = %(status)s,
                broker_order_id = COALESCE(%(broker_order_id)s, broker_order_id),
                broker_location = COALESCE(%(broker_location)s, broker_location),
                error = %(error)s,
                updated_at = NOW()
            WHERE idempotency_key = %(idempotency_key)s
            RETURNING *
            """,
            {
                "idempotency_key": key,
                "status": normalized_status,
                "broker_order_id": broker_order_id,
                "broker_location": broker_location,
                "error": error,
            },
        ).fetchone()
    if not row:
        raise KeyError(f"Unknown order intent: {key}")
    return dict(row)


def claim_order_intent_submission(idempotency_key: str) -> dict[str, Any] | None:
    """Atomically claim one reserved intent before making a non-idempotent broker call."""
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    key = validate_idempotency_key(idempotency_key)
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        row = conn.execute(
            """
            UPDATE trade_order_intents
            SET status = 'submitting', updated_at = NOW()
            WHERE idempotency_key = %(idempotency_key)s
              AND status = 'reserved'
            RETURNING *
            """,
            {"idempotency_key": key},
        ).fetchone()
    return dict(row) if row else None


def list_order_intents(*, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    account = str(account_id or "").strip()
    if not account:
        raise ValueError("account_id is required")
    bounded_limit = max(1, min(int(limit), 200))
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        rows = conn.execute(
            """
            SELECT idempotency_key, position_id, symbol, side, quantity, order_type, intent_role,
                   status, broker_order_id, broker_location, error, created_at, updated_at
            FROM trade_order_intents
            WHERE account_id = %(account_id)s
            ORDER BY updated_at DESC
            LIMIT %(limit)s
            """,
            {"account_id": account, "limit": bounded_limit},
        ).fetchall()
    output = []
    for source in rows:
        row = dict(source)
        for key in ("created_at", "updated_at"):
            if hasattr(row.get(key), "isoformat"):
                row[key] = row[key].isoformat()
        output.append(row)
    return output


def record_account_activity(activity: dict[str, Any]) -> dict[str, Any]:
    """Persist one normalized Schwab event and update a known order intent once."""
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    event_key = str(activity.get("event_key") or "").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", event_key):
        raise ValueError("invalid account activity event key")
    persistent_store.ensure_schema()
    params = {
        "event_key": event_key,
        "account_number": activity.get("account_number"),
        "sequence_number": activity.get("sequence_number"),
        "message_type": str(activity.get("message_type") or "UNKNOWN"),
        "broker_order_id": activity.get("broker_order_id"),
        "order_status": activity.get("order_status"),
        "event_ts": int(activity.get("event_ts") or 0),
        "payload": persistent_store.jsonb(activity.get("payload") or {}),
    }
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        inserted = conn.execute(
            """
            INSERT INTO schwab_account_activity (
                event_key, account_number, sequence_number, message_type,
                broker_order_id, order_status, event_ts, payload
            ) VALUES (
                %(event_key)s, %(account_number)s, %(sequence_number)s, %(message_type)s,
                %(broker_order_id)s, %(order_status)s, %(event_ts)s, %(payload)s
            )
            ON CONFLICT (event_key) DO NOTHING
            RETURNING event_key
            """,
            params,
        ).fetchone()
        matched_intents = 0
        if inserted and params["broker_order_id"] is not None and params["order_status"]:
            matched_intents = len(conn.execute(
                """
                UPDATE trade_order_intents
                SET status = %(order_status)s, updated_at = NOW()
                WHERE broker_order_id = %(broker_order_id)s
                  AND status NOT IN ('filled', 'canceled', 'rejected', 'failed')
                  AND (
                    %(order_status)s IN ('filled', 'canceled', 'rejected', 'failed')
                    OR (%(order_status)s = 'partially_filled'
                        AND status IN ('reserved', 'unknown', 'submitted', 'cancel_pending'))
                    OR (%(order_status)s = 'cancel_pending'
                        AND status IN ('reserved', 'unknown', 'submitted'))
                    OR (%(order_status)s = 'submitted'
                        AND status IN ('reserved', 'unknown'))
                  )
                RETURNING idempotency_key
                """,
                params,
            ).fetchall())
    return {
        "event_key": event_key,
        "created": inserted is not None,
        "matched_intents": matched_intents,
    }


def get_order_intent_by_broker_id(broker_order_id: int) -> dict[str, Any] | None:
    if not persistent_store.enabled():
        return None
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        row = conn.execute(
            "SELECT * FROM trade_order_intents WHERE broker_order_id = %(broker_order_id)s",
            {"broker_order_id": int(broker_order_id)},
        ).fetchone()
    return dict(row) if row else None


def record_order_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Store an authoritative REST order snapshot and any new execution fills."""
    if not persistent_store.enabled():
        raise RuntimeError("PostgreSQL trade journal is not configured")
    broker_order_id = int(snapshot.get("broker_order_id") or 0)
    if broker_order_id <= 0:
        raise ValueError("broker_order_id is required")
    persistent_store.ensure_schema()
    with persistent_store.psycopg.connect(
        persistent_store.DATABASE_URL,
        row_factory=persistent_store.dict_row,
    ) as conn:
        intent = conn.execute(
            "SELECT * FROM trade_order_intents WHERE broker_order_id = %(broker_order_id)s FOR UPDATE",
            {"broker_order_id": broker_order_id},
        ).fetchone()
        if not intent:
            return {"matched": False, "fills_created": 0}
        merged = merge_order_snapshot(dict(intent), snapshot)
        status = merged["status"]
        row = conn.execute(
            """
            UPDATE trade_order_intents
            SET status = %(status)s,
                filled_quantity = %(filled_quantity)s,
                remaining_quantity = %(remaining_quantity)s,
                average_fill_price = %(average_fill_price)s,
                last_broker_payload = %(payload)s,
                updated_at = NOW()
            WHERE idempotency_key = %(idempotency_key)s
            RETURNING position_id
            """,
            {
                "idempotency_key": intent["idempotency_key"],
                "status": status,
                "filled_quantity": merged["filled_quantity"],
                "remaining_quantity": merged["remaining_quantity"],
                "average_fill_price": merged["average_fill_price"],
                "payload": persistent_store.jsonb(merged["raw"]),
            },
        ).fetchone()
        position_id = (row or {}).get("position_id")
        fills_created = 0
        for fill in snapshot.get("fills") or []:
            inserted = conn.execute(
                """
                INSERT INTO trade_fills (
                    fill_key, idempotency_key, position_id, broker_order_id, leg_id,
                    symbol, instruction, quantity, price, executed_at
                ) VALUES (
                    %(fill_key)s, %(idempotency_key)s, %(position_id)s, %(broker_order_id)s,
                    %(leg_id)s, %(symbol)s, %(instruction)s, %(quantity)s, %(price)s,
                    %(executed_at)s
                )
                ON CONFLICT (fill_key) DO NOTHING
                RETURNING fill_key
                """,
                {**fill, "idempotency_key": intent["idempotency_key"], "position_id": position_id},
            ).fetchone()
            fills_created += 1 if inserted else 0
            if inserted and position_id:
                conn.execute(
                    """
                    INSERT INTO trade_position_events (
                        event_key, position_id, event_type, broker_order_id, event_ts, payload
                    ) VALUES (
                        %(event_key)s, %(position_id)s, 'fill', %(broker_order_id)s,
                        (EXTRACT(EPOCH FROM COALESCE(%(executed_at)s::timestamptz, NOW())) * 1000)::bigint,
                        %(payload)s
                    )
                    ON CONFLICT (event_key) DO NOTHING
                    """,
                    {
                        "event_key": fill["fill_key"],
                        "position_id": position_id,
                        "broker_order_id": broker_order_id,
                        "executed_at": fill.get("executed_at"),
                        "payload": persistent_store.jsonb(fill),
                    },
                )
        if position_id:
            position = conn.execute(
                "SELECT intended_quantity FROM trade_positions WHERE position_id = %(position_id)s FOR UPDATE",
                {"position_id": position_id},
            ).fetchone()
            all_fills = conn.execute(
                """
                SELECT instruction, quantity, price FROM trade_fills
                WHERE position_id = %(position_id)s
                ORDER BY executed_at NULLS LAST, created_at
                """,
                {"position_id": position_id},
            ).fetchall()
            terminal_unfilled = (
                str(intent.get("side") or "").upper() == "BUY"
                and status in {"canceled", "rejected", "failed"}
            )
            reduced = reduce_long_position_fills(
                [dict(fill) for fill in all_fills],
                intended_quantity=int((position or {}).get("intended_quantity") or 0),
                entry_terminal_without_fill=terminal_unfilled,
            )
            conn.execute(
                """
                UPDATE trade_positions
                SET state = %(state)s, filled_quantity = %(filled_quantity)s,
                    remaining_quantity = %(remaining_quantity)s,
                    average_entry_price = NULLIF(%(average_entry_price)s, 0),
                    partial_taken = %(partial_taken)s, realized_pnl = %(realized_pnl)s,
                    opened_at = CASE WHEN %(filled_quantity)s > 0 THEN COALESCE(opened_at, NOW()) ELSE opened_at END,
                    closed_at = CASE WHEN %(state)s = 'closed' THEN COALESCE(closed_at, NOW()) ELSE NULL END,
                    updated_at = NOW()
                WHERE position_id = %(position_id)s
                """,
                {**reduced, "position_id": position_id},
            )
    return {
        "matched": True,
        "idempotency_key": intent["idempotency_key"],
        "position_id": position_id,
        "fills_created": fills_created,
    }
