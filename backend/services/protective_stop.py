from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
import hashlib
import math
from typing import Any


ACTIVE_ORDER_STATUSES = frozenset({
    "reserved", "submitting", "unknown", "submitted", "partially_filled", "cancel_pending"
})


@dataclass(frozen=True)
class ProtectionDecision:
    action: str
    quantity: int
    stop_price: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def normalize_equity_stop_price(value: float) -> float:
    price = _number(value)
    if price <= 0:
        raise ValueError("stop price must be positive")
    increment = Decimal("0.0001") if price < 1 else Decimal("0.01")
    return float(Decimal(str(price)).quantize(increment, rounding=ROUND_DOWN))


def build_protective_stop_order(*, symbol: str, quantity: int, stop_price: float) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if int(quantity) < 1:
        raise ValueError("quantity must be at least one")
    normalized_stop = normalize_equity_stop_price(stop_price)
    return {
        "orderType": "STOP",
        "session": "NORMAL",
        "duration": "GOOD_TILL_CANCEL",
        "stopPrice": normalized_stop,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": "SELL",
            "quantity": int(quantity),
            "instrument": {"symbol": normalized_symbol, "assetType": "EQUITY"},
        }],
    }


def protection_idempotency_key(
    *,
    position_id: str,
    quantity: int,
    stop_price: float,
    replaces_idempotency_key: str | None = None,
) -> str:
    normalized_stop = normalize_equity_stop_price(stop_price)
    digest = hashlib.sha256(
        (
            f"{position_id}|{int(quantity)}|{normalized_stop:.4f}|"
            f"{replaces_idempotency_key or 'initial'}"
        ).encode("utf-8")
    ).hexdigest()
    return f"protect:{digest}"


def evaluate_protective_stop(
    position: dict[str, Any],
    protective_intent: dict[str, Any] | None,
) -> ProtectionDecision:
    state = str(position.get("state") or "").lower()
    remaining = math.floor(max(_number(position.get("remaining_quantity")), 0.0))
    entry = _number(position.get("average_entry_price"))
    stop = _number(position.get("active_stop_price") or position.get("hard_stop_price"))
    intent = protective_intent or {}
    intent_status = str(intent.get("status") or "").lower()
    intent_active = intent_status in ACTIVE_ORDER_STATUSES

    if state in {"closed", "blocked"} or remaining < 1:
        if intent_status == "cancel_pending":
            return ProtectionDecision("none", 0, stop, "cancellation_pending")
        if intent_status == "unknown":
            return ProtectionDecision("blocked", 0, stop, "protection_state_unknown")
        if intent_active:
            return ProtectionDecision("cancel", 0, stop, "position_no_longer_open")
        return ProtectionDecision("none", 0, stop, "position_no_longer_open")
    if state not in {"entry_pending", "open", "partial_taken", "runner"}:
        return ProtectionDecision("blocked", 0, stop, "unsupported_position_state")
    if entry <= 0:
        return ProtectionDecision("blocked", 0, stop, "entry_fill_not_confirmed")
    hard_stop = _number(position.get("hard_stop_price"))
    if hard_stop <= 0 or hard_stop >= entry or stop < hard_stop:
        return ProtectionDecision("blocked", 0, stop, "invalid_long_stop")

    normalized_stop = normalize_equity_stop_price(stop)
    if intent_status in {"reserved", "unknown", "cancel_pending"}:
        return ProtectionDecision("blocked", 0, normalized_stop, "protection_state_unknown")
    if not intent_active:
        return ProtectionDecision("create", remaining, normalized_stop, "protection_missing")

    protected_quantity = int(_number(intent.get("quantity")))
    payload = intent.get("order_payload") or {}
    protected_stop = _number(payload.get("stopPrice"))
    if protected_quantity != remaining or normalize_equity_stop_price(protected_stop) != normalized_stop:
        return ProtectionDecision("replace", remaining, normalized_stop, "protection_out_of_sync")
    return ProtectionDecision("none", remaining, normalized_stop, "protection_current")
