from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
from typing import Any

from services.trade_journal import (
    claim_order_intent_submission,
    reserve_order_intent,
    update_order_intent,
)


ACTIVE_EXIT_STATUSES = frozenset(
    {"reserved", "submitting", "unknown", "submitted", "partially_filled", "cancel_pending"}
)
TERMINAL_PROTECTION_STATUSES = frozenset({"canceled", "filled", "rejected", "failed"})


@dataclass(frozen=True)
class ExitCoordinationDecision:
    action: str
    reason: str
    quantity: int
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _quantity(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return math.floor(number) if math.isfinite(number) and number > 0 else 0


def exit_idempotency_key(
    *, position_id: str, quantity: int, action: str = "exit_all"
) -> str:
    digest = hashlib.sha256(
        f"{position_id}|{action}|{int(quantity)}".encode("utf-8")
    ).hexdigest()
    return f"exit:{digest}"


def build_market_exit_order(*, symbol: str, quantity: int) -> dict[str, Any]:
    wanted = str(symbol or "").strip().upper()
    if not wanted:
        raise ValueError("symbol is required")
    if int(quantity) < 1:
        raise ValueError("quantity must be at least one")
    return {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "SELL",
                "quantity": int(quantity),
                "instrument": {"symbol": wanted, "assetType": "EQUITY"},
            }
        ],
    }


def evaluate_exit_coordination(
    *,
    position: dict[str, Any],
    exit_decision: dict[str, Any],
    protective_intent: dict[str, Any] | None,
    exit_intent: dict[str, Any] | None,
) -> ExitCoordinationDecision:
    remaining = _quantity(position.get("remaining_quantity"))
    requested = _quantity(exit_decision.get("quantity"))
    action = str(exit_decision.get("action") or "hold").lower()
    if remaining < 1:
        return ExitCoordinationDecision("none", "position_not_open", 0)
    if action in {"hold", "blocked"}:
        return ExitCoordinationDecision("none", "no_exit_requested", 0)
    if action == "take_partial":
        if requested < 1 or requested >= remaining:
            return ExitCoordinationDecision(
                "blocked", "invalid_partial_exit_quantity", requested
            )
    elif action == "exit_all":
        if requested != remaining:
            return ExitCoordinationDecision(
                "blocked", "invalid_full_exit_quantity", requested
            )
    else:
        return ExitCoordinationDecision("blocked", "unsupported_exit_action", requested)

    key = exit_idempotency_key(
        position_id=str(position.get("position_id") or ""),
        quantity=requested,
        action=action,
    )
    protection = protective_intent or {}
    protection_status = str(protection.get("status") or "").lower()
    if (
        action == "take_partial"
        and protection
        and protection_status not in TERMINAL_PROTECTION_STATUSES
    ):
        return ExitCoordinationDecision(
            "blocked", "standing_broker_order_conflicts_with_local_partial", requested, key
        )
    existing_exit = exit_intent or {}
    exit_status = str(existing_exit.get("status") or "").lower()
    if existing_exit:
        same_intent = str(existing_exit.get("idempotency_key") or "") == key
        if not same_intent and exit_status in ACTIVE_EXIT_STATUSES:
            return ExitCoordinationDecision(
                "wait", "different_exit_in_progress", requested, key
            )
        if same_intent and exit_status in {"submitting", "unknown", "submitted", "partially_filled", "cancel_pending"}:
            return ExitCoordinationDecision("wait", "exit_in_progress", requested, key)
        if same_intent and exit_status == "filled":
            return ExitCoordinationDecision(
                "wait", "exit_fill_awaiting_position_reconciliation", requested, key
            )
        if same_intent and exit_status in {"canceled", "rejected", "failed"}:
            return ExitCoordinationDecision(
                "blocked", "prior_exit_terminal_failure", requested, key
            )
        if same_intent and exit_status != "reserved":
            return ExitCoordinationDecision("blocked", "exit_state_unknown", requested, key)
        if not same_intent:
            existing_exit = {}

    if not existing_exit:
        reason = "partial_exit_requested" if action == "take_partial" else "full_exit_requested"
        return ExitCoordinationDecision("reserve_exit", reason, requested, key)

    if not protection or protection_status in TERMINAL_PROTECTION_STATUSES:
        return ExitCoordinationDecision(
            "submit_exit", "protection_not_active", requested, key
        )
    if protection_status in {"reserved", "submitting", "unknown", "cancel_pending"}:
        return ExitCoordinationDecision(
            "wait", "protection_state_ambiguous", requested, key
        )
    if protection_status in {"submitted", "partially_filled"}:
        if not protection.get("broker_order_id"):
            return ExitCoordinationDecision(
                "blocked", "protective_order_id_missing", requested, key
            )
        return ExitCoordinationDecision(
            "cancel_protection", "full_exit_requires_stop_cancel", requested, key
        )
    return ExitCoordinationDecision("blocked", "protection_state_unknown", requested, key)


def synchronize_exit(
    *,
    position: dict[str, Any],
    exit_decision: dict[str, Any],
    protective_intent: dict[str, Any] | None,
    exit_intent: dict[str, Any] | None,
    order_client: Any | None,
    execute: bool,
) -> dict[str, Any]:
    plan = evaluate_exit_coordination(
        position=position,
        exit_decision=exit_decision,
        protective_intent=protective_intent,
        exit_intent=exit_intent,
    )
    result: dict[str, Any] = {"decision": plan.to_dict(), "executed": False}
    if not execute or plan.action in {"none", "blocked", "wait"}:
        return result

    payload = build_market_exit_order(
        symbol=position["symbol"], quantity=plan.quantity
    )
    result["order_payload"] = payload
    if plan.action == "reserve_exit":
        reservation = reserve_order_intent(
            idempotency_key=plan.idempotency_key,
            position_id=position["position_id"],
            account_id=position["account_id"],
            symbol=position["symbol"],
            side="SELL",
            quantity=plan.quantity,
            order_type="MARKET",
            order_payload=payload,
            intent_role="exit",
        )
        return {
            **result,
            "executed": True,
            "reserved": reservation.created,
            "status": reservation.intent.get("status"),
        }

    if plan.action == "cancel_protection":
        protection = protective_intent or {}
        update_order_intent(protection["idempotency_key"], status="cancel_pending")
        try:
            response = order_client.cancel_order(
                position["account_id"], int(protection["broker_order_id"])
            )
        except Exception as exc:
            update_order_intent(
                protection["idempotency_key"], status="unknown", error=str(exc)
            )
            raise
        return {
            **result,
            "executed": True,
            "status": "cancel_pending",
            "order_response": response,
        }

    claimed = claim_order_intent_submission(plan.idempotency_key)
    if not claimed:
        return {**result, "replayed": True, "status": "not_claimed"}
    try:
        response = order_client.place_order(position["account_id"], payload)
    except Exception as exc:
        update_order_intent(plan.idempotency_key, status="unknown", error=str(exc))
        raise
    if not (response or {}).get("order_id"):
        update_order_intent(
            plan.idempotency_key,
            status="unknown",
            error="broker_order_id_missing",
        )
        return {
            **result,
            "executed": True,
            "status": "unknown",
            "order_response": response,
        }
    update_order_intent(
        plan.idempotency_key,
        status="submitted",
        broker_order_id=response["order_id"],
        broker_location=response.get("location"),
    )
    return {
        **result,
        "executed": True,
        "status": "submitted",
        "order_response": response,
    }


# Kept for callers that used the earlier full-exit-only name.
synchronize_full_exit = synchronize_exit
