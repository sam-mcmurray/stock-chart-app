from __future__ import annotations

from typing import Any

from services.protective_stop import (
    build_protective_stop_order,
    evaluate_protective_stop,
    protection_idempotency_key,
)
from services.trade_journal import reserve_order_intent, update_order_intent


def synchronize_protective_stop(
    *,
    position: dict[str, Any],
    protective_intent: dict[str, Any] | None,
    order_client: Any | None,
    execute: bool,
    exit_intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile one broker-held stop without blindly retrying ambiguous calls."""
    decision = evaluate_protective_stop(position, protective_intent)
    result: dict[str, Any] = {"decision": decision.to_dict(), "executed": False}
    if str((exit_intent or {}).get("status") or "").lower() in {
        "reserved",
        "submitting",
        "unknown",
        "submitted",
        "partially_filled",
        "cancel_pending",
    }:
        return {
            **result,
            "decision": {
                **decision.to_dict(),
                "action": "blocked",
                "reason": "exit_in_progress",
            },
        }
    if decision.action in {"none", "blocked"}:
        return result

    existing = protective_intent or {}
    if decision.action == "cancel":
        broker_order_id = existing.get("broker_order_id")
        if not broker_order_id:
            result["decision"] = {
                **decision.to_dict(),
                "action": "blocked",
                "reason": "broker_order_id_missing",
            }
            return result
        if not execute:
            return result
        update_order_intent(existing["idempotency_key"], status="cancel_pending")
        try:
            response = order_client.cancel_order(position["account_id"], int(broker_order_id))
        except Exception as exc:
            update_order_intent(existing["idempotency_key"], status="unknown", error=str(exc))
            raise
        return {
            **result,
            "executed": True,
            "status": "cancel_pending",
            "order_response": response,
        }

    payload = build_protective_stop_order(
        symbol=position["symbol"],
        quantity=decision.quantity,
        stop_price=decision.stop_price,
    )
    result["order_payload"] = payload
    if not execute:
        return result

    key = protection_idempotency_key(
        position_id=position["position_id"],
        quantity=decision.quantity,
        stop_price=decision.stop_price,
        replaces_idempotency_key=existing.get("idempotency_key"),
    )
    reservation = reserve_order_intent(
        idempotency_key=key,
        position_id=position["position_id"],
        account_id=position["account_id"],
        symbol=position["symbol"],
        side="SELL",
        quantity=decision.quantity,
        order_type="STOP",
        order_payload=payload,
        intent_role="protective_stop",
        replaces_idempotency_key=existing.get("idempotency_key"),
    )
    result["idempotency_key"] = key
    if not reservation.created:
        return {
            **result,
            "replayed": True,
            "status": reservation.intent.get("status"),
        }

    if decision.action == "replace":
        old_order_id = existing.get("broker_order_id")
        if not old_order_id:
            update_order_intent(key, status="failed", error="broker_order_id_missing")
            result["decision"] = {
                **decision.to_dict(),
                "action": "blocked",
                "reason": "broker_order_id_missing",
            }
            return result
        update_order_intent(existing["idempotency_key"], status="cancel_pending")

    try:
        if decision.action == "replace":
            response = order_client.replace_order(
                position["account_id"], int(existing["broker_order_id"]), payload
            )
        else:
            response = order_client.place_order(position["account_id"], payload)
    except Exception as exc:
        update_order_intent(key, status="unknown", error=str(exc))
        if decision.action == "replace":
            update_order_intent(existing["idempotency_key"], status="unknown", error=str(exc))
        raise

    if decision.action == "replace":
        update_order_intent(existing["idempotency_key"], status="canceled")
    if not (response or {}).get("order_id"):
        update_order_intent(key, status="unknown", error="broker_order_id_missing")
        return {
            **result,
            "executed": True,
            "status": "unknown",
            "order_response": response,
        }
    update_order_intent(
        key,
        status="submitted",
        broker_order_id=(response or {}).get("order_id"),
        broker_location=(response or {}).get("location"),
    )
    return {**result, "executed": True, "order_response": response}
