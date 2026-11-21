from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Iterator


ORDER_ID_KEYS = ("orderId", "orderID", "order_id")
STATUS_KEYS = ("status", "orderStatus", "order_status")
STATUS_MAP = {
    "ACCEPTED": "submitted",
    "AWAITING_PARENT_ORDER": "submitted",
    "AWAITING_UR_OUT": "cancel_pending",
    "CANCELED": "canceled",
    "CANCELLED": "canceled",
    "EXPIRED": "canceled",
    "FILLED": "filled",
    "PARTIALLY_FILLED": "partially_filled",
    "PENDING_ACTIVATION": "submitted",
    "PENDING_CANCEL": "cancel_pending",
    "PENDING_REPLACE": "submitted",
    "QUEUED": "submitted",
    "REJECTED": "rejected",
    "REPLACED": "submitted",
    "WORKING": "submitted",
}


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_value(payload: Any, keys: tuple[str, ...]) -> Any:
    for item in _walk(payload):
        for key in keys:
            value = item.get(key)
            if value is not None and value != "":
                return value
    return None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decode_message_data(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {"raw_text": value}


def normalize_account_activity(row: dict[str, Any], *, packet_ts: int | None = None) -> dict[str, Any]:
    sequence = _integer(row.get("seq"))
    account_number = row.get("1") or row.get("account")
    message_type = str(row.get("2") or row.get("messageType") or "UNKNOWN").strip().upper()
    message_data = _decode_message_data(row.get("3", row.get("messageData")))
    broker_order_id = _integer(_first_value(message_data, ORDER_ID_KEYS))
    raw_status = _first_value(message_data, STATUS_KEYS)
    normalized_status = STATUS_MAP.get(str(raw_status or "").strip().upper())
    event_ts = _integer(packet_ts) or int(time.time() * 1000)
    canonical = json.dumps(
        {
            "account_number": account_number,
            "sequence_number": sequence,
            "message_type": message_type,
            "message_data": message_data,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    event_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "event_key": event_key,
        "account_number": str(account_number) if account_number is not None else None,
        "sequence_number": sequence,
        "message_type": message_type,
        "broker_order_id": broker_order_id,
        "order_status": normalized_status,
        "event_ts": event_ts,
        "payload": {
            "stream_row": row,
            "message_data": message_data,
            "raw_order_status": raw_status,
        },
    }
