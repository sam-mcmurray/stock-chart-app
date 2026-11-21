from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from services.account_activity import STATUS_MAP


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fill_key(order_id: int, leg_id: int, fill: dict[str, Any]) -> str:
    identity = json.dumps(
        {
            "order_id": order_id,
            "leg_id": leg_id,
            "time": fill.get("time"),
            "quantity": _number(fill.get("quantity")),
            "price": _number(fill.get("price")),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def normalize_schwab_order(order: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Schwab Order response without inferring missing executions."""
    order_id = _integer(order.get("orderId"))
    if order_id is None:
        raise ValueError("Schwab order is missing orderId")

    raw_status = str(order.get("status") or "").strip().upper()
    status = STATUS_MAP.get(raw_status, "unknown")
    legs: dict[int, dict[str, Any]] = {}
    for index, raw_leg in enumerate(order.get("orderLegCollection") or []):
        leg_id = _integer(raw_leg.get("legId"))
        if leg_id is None:
            leg_id = index
        instrument = raw_leg.get("instrument") or {}
        legs[leg_id] = {
            "leg_id": leg_id,
            "symbol": str(instrument.get("symbol") or "").upper() or None,
            "asset_type": instrument.get("assetType"),
            "instruction": str(raw_leg.get("instruction") or "").upper() or None,
            "quantity": _number(raw_leg.get("quantity")),
        }

    fills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for activity in order.get("orderActivityCollection") or []:
        if str(activity.get("activityType") or "").upper() != "EXECUTION":
            continue
        if str(activity.get("executionType") or "").upper() != "FILL":
            continue
        for raw_fill in activity.get("executionLegs") or []:
            leg_id = _integer(raw_fill.get("legId"))
            if leg_id is None or leg_id not in legs:
                continue
            quantity = _number(raw_fill.get("quantity"))
            price = _number(raw_fill.get("price"))
            if quantity <= 0 or price <= 0:
                continue
            key = _fill_key(order_id, leg_id, raw_fill)
            if key in seen:
                continue
            seen.add(key)
            fills.append({
                "fill_key": key,
                "broker_order_id": order_id,
                "leg_id": leg_id,
                "symbol": legs[leg_id]["symbol"],
                "instruction": legs[leg_id]["instruction"],
                "quantity": quantity,
                "price": price,
                "executed_at": raw_fill.get("time"),
            })

    filled_quantity = _number(order.get("filledQuantity"))
    remaining_quantity = _number(order.get("remainingQuantity"))
    if filled_quantity <= 0 and fills:
        filled_quantity = sum(fill["quantity"] for fill in fills)
    average_fill_price = 0.0
    fill_quantity = sum(fill["quantity"] for fill in fills)
    if fill_quantity > 0:
        average_fill_price = sum(fill["price"] * fill["quantity"] for fill in fills) / fill_quantity

    return {
        "broker_order_id": order_id,
        "raw_status": raw_status,
        "status": status,
        "order_type": str(order.get("orderType") or "").strip().upper() or None,
        "quantity": _number(order.get("quantity")),
        "filled_quantity": filled_quantity,
        "remaining_quantity": remaining_quantity,
        "average_fill_price": average_fill_price,
        "entered_time": order.get("enteredTime"),
        "close_time": order.get("closeTime"),
        "legs": list(legs.values()),
        "fills": fills,
        "raw": order,
    }


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ambiguous_order_query_window(
    intent: dict[str, Any], *, margin_seconds: int = 120
) -> tuple[str, str]:
    """Build the narrow Schwab query window around a possibly lost POST response."""
    created = _timestamp(intent.get("created_at"))
    updated = _timestamp(intent.get("updated_at"))
    if created is None or updated is None:
        raise ValueError("ambiguous intent is missing journal timestamps")
    margin = timedelta(seconds=max(1, int(margin_seconds)))
    start = created - margin
    end = updated + margin
    if end < start:
        raise ValueError("ambiguous intent timestamps are invalid")
    schwab_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    return start.strftime(schwab_format), end.strftime(schwab_format)


def match_ambiguous_order(
    intent: dict[str, Any], raw_orders: list[dict[str, Any]], *, margin_seconds: int = 120
) -> dict[str, Any] | None:
    """Return one exact broker match; never choose among multiple candidates."""
    created = _timestamp(intent.get("created_at"))
    updated = _timestamp(intent.get("updated_at"))
    if created is None or updated is None:
        return None
    margin = timedelta(seconds=max(1, int(margin_seconds)))
    earliest = created - margin
    latest = updated + margin
    symbol = str(intent.get("symbol") or "").upper()
    side = str(intent.get("side") or "").upper()
    order_type = str(intent.get("order_type") or "").upper()
    quantity = _number(intent.get("quantity"), default=-1)
    if not isinstance(raw_orders, list):
        return None
    candidates: list[dict[str, Any]] = []
    for raw_order in raw_orders or []:
        try:
            normalized = normalize_schwab_order(raw_order)
        except (AttributeError, TypeError, ValueError):
            continue
        entered = _timestamp(normalized.get("entered_time"))
        legs = normalized.get("legs") or []
        matching_quantity = sum(
            _number(leg.get("quantity"))
            for leg in legs
            if leg.get("symbol") == symbol and leg.get("instruction") == side
        )
        if (
            entered is not None
            and earliest <= entered <= latest
            and normalized.get("order_type") == order_type
            and math.isclose(matching_quantity, quantity, rel_tol=0, abs_tol=1e-9)
            and len(legs) == 1
        ):
            candidates.append(raw_order)
    return candidates[0] if len(candidates) == 1 else None
