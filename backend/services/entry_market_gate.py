from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any


@dataclass(frozen=True)
class EntryMarketLimits:
    max_quote_age_seconds: float = 3.0
    max_spread_bps: float = 75.0
    min_bid_size: int = 1
    min_ask_size: int = 1
    min_bid_notional: float = 1_000.0
    min_ask_notional: float = 1_000.0
    min_total_volume: int = 25_000
    min_price: float = 1.0
    max_price: float = 50.0


@dataclass(frozen=True)
class EntryMarketDecision:
    allowed: bool
    reasons: list[str]
    price: float
    quote_age_seconds: float | None
    spread_bps: float | None
    session: str
    security_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def normalize_schwab_equity_quote(
    payload: dict[str, Any],
    *,
    symbol: str,
    fetched_at_ms: int,
) -> dict[str, Any]:
    wanted = symbol.upper()
    instrument = payload.get(wanted) if isinstance(payload, dict) else None
    if not isinstance(instrument, dict):
        instrument = payload if isinstance(payload, dict) else {}
    quote = instrument.get("quote") if isinstance(instrument.get("quote"), dict) else {}
    try:
        volume = float(quote.get("totalVolume"))
        volume_valid = not isinstance(quote.get("totalVolume"), bool) and math.isfinite(volume) and volume >= 0
    except (TypeError, ValueError, OverflowError):
        volume_valid = False
    return {
        "symbol": wanted,
        "realtime": instrument.get("realtime") is True,
        "asset_main_type": str(instrument.get("assetMainType") or "").upper(),
        "bid": _number(quote.get("bidPrice")),
        "ask": _number(quote.get("askPrice")),
        "bid_size": math.floor(max(_number(quote.get("bidSize")), 0)),
        "ask_size": math.floor(max(_number(quote.get("askSize")), 0)),
        "last": _number(quote.get("lastPrice")),
        "mark": _number(quote.get("mark")),
        "quote_time_ms": math.floor(_number(quote.get("quoteTime"))),
        "trade_time_ms": math.floor(_number(quote.get("tradeTime"))),
        "security_status": str(quote.get("securityStatus") or "").strip(),
        "total_volume": math.floor(max(_number(quote.get("totalVolume")), 0)),
        "total_volume_valid": volume_valid,
        "fetched_at_ms": int(fetched_at_ms),
        "source": "schwab",
    }


def normalize_schwab_equity_hours(
    payload: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    equity = payload.get("equity") if isinstance(payload, dict) else {}
    equity = equity if isinstance(equity, dict) else {}
    product = equity.get("EQ") if isinstance(equity.get("EQ"), dict) else {}
    intervals = (product.get("sessionHours") or {}).get("regularMarket") or []
    current = now.astimezone(timezone.utc)
    in_regular = False
    for interval in intervals:
        try:
            start = datetime.fromisoformat(str(interval["start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(interval["end"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if start.astimezone(timezone.utc) <= current < end.astimezone(timezone.utc):
            in_regular = True
            break
    return {
        "market_open": product.get("isOpen") is True,
        "is_regular": in_regular,
        "session": "regular" if in_regular else "closed",
        "date": product.get("date"),
    }


def evaluate_entry_market_gate(
    *,
    quote: dict[str, Any],
    market_hours: dict[str, Any],
    now_ms: int,
    limits: EntryMarketLimits | None = None,
    heuristic_halt_risk: bool = False,
) -> EntryMarketDecision:
    limits = limits or EntryMarketLimits()
    reasons: list[str] = []
    bid = _number(quote.get("bid"))
    ask = _number(quote.get("ask"))
    mark = _number(quote.get("mark"))
    last = _number(quote.get("last"))
    price = mark if mark > 0 else (bid + ask) / 2 if bid > 0 and ask > 0 else last
    quote_time_ms = int(_number(quote.get("quote_time_ms")))
    raw_age = (int(now_ms) - quote_time_ms) / 1000 if quote_time_ms > 0 else None
    age = max(raw_age, 0.0) if raw_age is not None else None
    spread_bps = None
    if bid > 0 and ask > 0 and ask >= bid:
        midpoint = (bid + ask) / 2
        spread_bps = (ask - bid) / midpoint * 10_000 if midpoint > 0 else None

    if quote.get("realtime") is not True:
        reasons.append("quote_not_realtime")
    if str(quote.get("asset_main_type") or "").upper() != "EQUITY":
        reasons.append("not_equity")
    if quote_time_ms <= 0:
        reasons.append("quote_timestamp_missing")
    elif raw_age is not None and (raw_age < -5 or raw_age > limits.max_quote_age_seconds):
        reasons.append("quote_stale")
    if bid <= 0 or ask <= 0 or ask < bid:
        reasons.append("invalid_nbbo")
    elif spread_bps is None or spread_bps > limits.max_spread_bps:
        reasons.append("spread_too_wide")
    if int(_number(quote.get("bid_size"))) < limits.min_bid_size:
        reasons.append("bid_liquidity_too_low")
    if int(_number(quote.get("ask_size"))) < limits.min_ask_size:
        reasons.append("ask_liquidity_too_low")
    if bid * int(_number(quote.get("bid_size"))) < limits.min_bid_notional:
        reasons.append("bid_notional_too_low")
    if ask * int(_number(quote.get("ask_size"))) < limits.min_ask_notional:
        reasons.append("ask_notional_too_low")
    if int(_number(quote.get("total_volume"))) < limits.min_total_volume:
        reasons.append("day_volume_too_low")
    if price < limits.min_price or price > limits.max_price:
        reasons.append("price_out_of_range")
    security_status = str(quote.get("security_status") or "").strip()
    if security_status.lower() != "normal":
        reasons.append("security_not_normal")
    if heuristic_halt_risk:
        reasons.append("halt_risk")
    if market_hours.get("market_open") is not True or market_hours.get("is_regular") is not True:
        reasons.append("market_not_regular")

    return EntryMarketDecision(
        allowed=not reasons,
        reasons=reasons,
        price=round(price, 4),
        quote_age_seconds=round(age, 3) if age is not None else None,
        spread_bps=round(spread_bps, 3) if spread_bps is not None else None,
        session=str(market_hours.get("session") or "unknown"),
        security_status=security_status or "unknown",
    )
