from __future__ import annotations

import json
import math
import time
from typing import Any


def _loads(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def select_listing_book(
    levelone: dict[str, Any] | None,
    nasdaq_book: dict[str, Any] | None,
    nyse_book: dict[str, Any] | None,
) -> dict[str, Any] | None:
    exchange = str((levelone or {}).get("exchange_id") or "").upper()
    preferred = nasdaq_book if exchange == "Q" else nyse_book if exchange in {"N", "A", "P"} else None
    if preferred:
        return preferred
    candidates = [book for book in (nasdaq_book, nyse_book) if book]
    if not candidates:
        return None
    return max(candidates, key=lambda book: int(_number(book.get("market_time_ms"))))


def book_metrics(book: dict[str, Any] | None, *, now_ms: int, levels: int = 5) -> dict[str, Any]:
    book = book or {}
    bids = (book.get("bids") or [])[:max(1, levels)]
    asks = (book.get("asks") or [])[:max(1, levels)]
    bid_depth = sum(max(_number(row.get("size")), 0.0) for row in bids)
    ask_depth = sum(max(_number(row.get("size")), 0.0) for row in asks)
    total_depth = bid_depth + ask_depth
    market_time_ms = int(_number(book.get("market_time_ms")))
    received_at_ms = int(_number(book.get("received_at_ms")))
    return {
        "service": book.get("service"),
        "market_time_ms": market_time_ms or None,
        "market_age_seconds": round(max((now_ms - market_time_ms) / 1000, 0), 3) if market_time_ms else None,
        "receive_age_seconds": round(max((now_ms - received_at_ms) / 1000, 0), 3) if received_at_ms else None,
        "best_bid": _number(bids[0].get("price")) if bids else None,
        "best_ask": _number(asks[0].get("price")) if asks else None,
        "bid_depth": int(bid_depth),
        "ask_depth": int(ask_depth),
        "imbalance": round((bid_depth - ask_depth) / total_depth, 4) if total_depth else None,
        "levels": max(1, levels),
    }


def load_market_stream_snapshot(
    redis_client: Any,
    *,
    symbol: str,
    levelone_key: str,
    nasdaq_book_key: str,
    nyse_book_key: str,
    chart_key_prefix: str,
    chart_limit: int = 390,
    now_ms: int | None = None,
) -> dict[str, Any]:
    wanted = symbol.upper()
    levelone = _loads(redis_client.hget(levelone_key, wanted))
    nasdaq = _loads(redis_client.hget(nasdaq_book_key, wanted))
    nyse = _loads(redis_client.hget(nyse_book_key, wanted))
    selected = select_listing_book(levelone, nasdaq, nyse)
    raw_candles = redis_client.hgetall(f"{chart_key_prefix}{wanted}") or {}
    candles = [_loads(value) for value in raw_candles.values()]
    candles = sorted(
        (row for row in candles if row),
        key=lambda row: int(_number(row.get("chart_time_ms"))),
    )[-max(1, min(int(chart_limit), 1000)):]
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    received = int(_number((levelone or {}).get("received_at_ms")))
    quote_time = int(_number((levelone or {}).get("quote_time_ms")))
    return {
        "symbol": wanted,
        "levelone": levelone,
        "levelone_receive_age_seconds": round(max((current_ms - received) / 1000, 0), 3) if received else None,
        "quote_age_seconds": round(max((current_ms - quote_time) / 1000, 0), 3) if quote_time else None,
        "book": selected,
        "book_metrics": book_metrics(selected, now_ms=current_ms),
        "candles": candles,
        "candle_count": len(candles),
    }
