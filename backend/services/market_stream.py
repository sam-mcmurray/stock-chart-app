from __future__ import annotations

import math
from typing import Any


LEVELONE_FIELDS = "0,1,2,3,4,5,8,13,27,28,32,33,34,35,37,38,42"
CHART_FIELDS = "0,1,2,3,4,5,6,7,8"
BOOK_FIELDS = "0,1,2,3"


def _value(row: Any, key: int, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(str(key), row.get(key, default))
    if isinstance(row, (list, tuple)) and 0 <= key < len(row):
        return row[key]
    return default


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _integer(value: Any) -> int:
    return math.floor(_number(value))


def _present(row: dict[str, Any], key: int) -> bool:
    return str(key) in row or key in row


def _optional_number(row: dict[str, Any], key: int) -> float | None:
    return _number(_value(row, key)) if _present(row, key) else None


def _optional_integer(row: dict[str, Any], key: int) -> int | None:
    return _integer(_value(row, key)) if _present(row, key) else None


def normalize_levelone_equity(row: dict[str, Any], *, received_at_ms: int) -> dict[str, Any]:
    symbol = str(row.get("key") or _value(row, 0) or "").upper()
    bid_lots = _optional_integer(row, 4)
    ask_lots = _optional_integer(row, 5)
    bid_lots = max(bid_lots, 0) if bid_lots is not None else None
    ask_lots = max(ask_lots, 0) if ask_lots is not None else None
    return {
        "symbol": symbol,
        "bid": _optional_number(row, 1),
        "ask": _optional_number(row, 2),
        "last": _optional_number(row, 3),
        "bid_size_lots": bid_lots,
        "ask_size_lots": ask_lots,
        "bid_size_shares": bid_lots * 100 if bid_lots is not None else None,
        "ask_size_shares": ask_lots * 100 if ask_lots is not None else None,
        "total_volume": max(_optional_integer(row, 8), 0) if _present(row, 8) else None,
        "exchange_id": str(_value(row, 13) or "") if _present(row, 13) else None,
        "regular_market_quote": _value(row, 27) if _present(row, 27) else None,
        "regular_market_trade": _value(row, 28) if _present(row, 28) else None,
        "security_status": str(_value(row, 32) or "") if _present(row, 32) else None,
        "mark": _optional_number(row, 33),
        "quote_time_ms": _optional_integer(row, 34),
        "trade_time_ms": _optional_integer(row, 35),
        "bid_time_ms": _optional_integer(row, 37),
        "ask_time_ms": _optional_integer(row, 38),
        "net_percent_change": _optional_number(row, 42),
        "delayed": row.get("delayed") is True if "delayed" in row else None,
        "asset_main_type": str(row.get("assetMainType") or "").upper() if "assetMainType" in row else None,
        "received_at_ms": int(received_at_ms),
        "source": "schwab_stream",
    }


def merge_levelone_equity(
    previous: dict[str, Any] | None, update: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(previous or {})
    merged.update({key: value for key, value in update.items() if value is not None})
    return merged


def normalize_chart_equity(row: dict[str, Any], *, received_at_ms: int) -> dict[str, Any]:
    # Observed production wire data uses 1=sequence, 2-6=OHLCV.
    return {
        "symbol": str(row.get("key") or _value(row, 0) or "").upper(),
        "sequence": _integer(_value(row, 1)),
        "open": _number(_value(row, 2)),
        "high": _number(_value(row, 3)),
        "low": _number(_value(row, 4)),
        "close": _number(_value(row, 5)),
        "volume": max(_number(_value(row, 6)), 0.0),
        "chart_time_ms": _integer(_value(row, 7)),
        "chart_day": _integer(_value(row, 8)),
        "received_at_ms": int(received_at_ms),
        "source": "schwab_stream",
    }


def _normalize_book_levels(raw_levels: Any) -> list[dict[str, Any]]:
    output = []
    for level in raw_levels if isinstance(raw_levels, list) else []:
        price = _number(_value(level, 0))
        size = max(_integer(_value(level, 1)), 0)
        if price <= 0 or size <= 0:
            continue
        makers = []
        raw_makers = _value(level, 3, [])
        for maker in raw_makers if isinstance(raw_makers, list) else []:
            makers.append({
                "id": str(_value(maker, 0) or ""),
                "size": max(_integer(_value(maker, 1)), 0),
                "quote_time_ms": _integer(_value(maker, 2)),
            })
        output.append({
            "price": price,
            "size": size,
            "market_maker_count": max(_integer(_value(level, 2)), 0),
            "market_makers": makers,
        })
    return output


def normalize_equity_book(
    row: dict[str, Any], *, service: str, received_at_ms: int
) -> dict[str, Any]:
    return {
        "symbol": str(row.get("key") or _value(row, 0) or "").upper(),
        "service": service,
        "market_time_ms": _integer(_value(row, 1)),
        "bids": _normalize_book_levels(_value(row, 2, [])),
        "asks": _normalize_book_levels(_value(row, 3, [])),
        "received_at_ms": int(received_at_ms),
        "source": "schwab_stream",
    }


def response_errors(responses: Any) -> list[dict[str, Any]]:
    errors = []
    for response in responses if isinstance(responses, list) else []:
        content = response.get("content") or {}
        try:
            code = int(content.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            errors.append({
                "service": response.get("service"),
                "command": response.get("command"),
                "code": code,
                "message": content.get("msg"),
            })
    return errors
