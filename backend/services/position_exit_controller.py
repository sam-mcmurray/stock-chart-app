from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

from services.trade_risk import ExitPolicy, evaluate_long_exit


@dataclass(frozen=True)
class ExitStreamLimits:
    max_quote_age_seconds: float = 3.0
    max_receive_age_seconds: float = 3.0
    max_book_age_seconds: float = 2.0
    max_book_receive_age_seconds: float = 2.0


@dataclass(frozen=True)
class PositionExitEvaluation:
    action: str
    quantity: int
    reason: str | None
    r_multiple: float
    active_stop: float
    peak_price: float
    current_price: float
    quote_is_stale: bool
    momentum_reversal: bool
    order_flow_imbalance: float | None
    order_flow_fresh: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _current_price(levelone: dict[str, Any]) -> float:
    last = _number(levelone.get("last"))
    if last > 0:
        return last
    mark = _number(levelone.get("mark"))
    if mark > 0:
        return mark
    bid = _number(levelone.get("bid"))
    ask = _number(levelone.get("ask"))
    return (bid + ask) / 2 if bid > 0 and ask >= bid else 0.0


def candle_momentum_reversal(candles: list[dict[str, Any]], *, current_price: float) -> bool:
    """Require consecutive bearish, lower-closing completed candles plus price confirmation."""
    valid = [
        row
        for row in candles
        if _number(row.get("open")) > 0
        and _number(row.get("high")) > 0
        and _number(row.get("low")) > 0
        and _number(row.get("close")) > 0
    ]
    if len(valid) < 3 or current_price <= 0:
        return False
    prior, middle, latest = valid[-3:]
    middle_close = _number(middle.get("close"))
    latest_close = _number(latest.get("close"))
    return bool(
        middle_close < _number(middle.get("open"))
        and latest_close < _number(latest.get("open"))
        and middle_close < _number(prior.get("close"))
        and latest_close < middle_close
        and _number(latest.get("high")) <= _number(middle.get("high"))
        and current_price <= latest_close
    )


def evaluate_position_exit(
    position: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    trading_disabled: bool = False,
    policy: ExitPolicy | None = None,
    limits: ExitStreamLimits | None = None,
) -> PositionExitEvaluation:
    limits = limits or ExitStreamLimits()
    levelone = snapshot.get("levelone") or {}
    current_price = _current_price(levelone)
    quote_age = snapshot.get("quote_age_seconds")
    receive_age = snapshot.get("levelone_receive_age_seconds")
    quote_is_stale = bool(
        not levelone
        or current_price <= 0
        or levelone.get("delayed") is True
        or str(levelone.get("security_status") or "").lower() not in {"normal", ""}
        or quote_age is None
        or _number(quote_age) > limits.max_quote_age_seconds
        or receive_age is None
        or _number(receive_age) > limits.max_receive_age_seconds
    )

    metrics = snapshot.get("book_metrics") or {}
    book_age = metrics.get("market_age_seconds")
    book_receive_age = metrics.get("receive_age_seconds")
    imbalance = metrics.get("imbalance")
    order_flow_fresh = bool(
        metrics.get("service") in {"NASDAQ_BOOK", "NYSE_BOOK"}
        and book_age is not None
        and _number(book_age) <= limits.max_book_age_seconds
        and book_receive_age is not None
        and _number(book_receive_age) <= limits.max_book_receive_age_seconds
        and imbalance is not None
    )
    usable_imbalance = _number(imbalance) if order_flow_fresh else None
    momentum_reversal = candle_momentum_reversal(
        snapshot.get("candles") or [],
        current_price=current_price,
    )
    peak_price = max(
        _number(position.get("peak_price")),
        _number(position.get("average_entry_price")),
        current_price,
    )
    decision = evaluate_long_exit(
        quantity=int(_number(position.get("remaining_quantity"))),
        entry_price=_number(position.get("average_entry_price")),
        hard_stop_price=_number(position.get("hard_stop_price")),
        active_stop_price=_number(position.get("active_stop_price")),
        current_price=current_price,
        peak_price=peak_price,
        partial_taken=bool(position.get("partial_taken")),
        quote_is_stale=quote_is_stale,
        trading_disabled=trading_disabled,
        momentum_reversal=momentum_reversal,
        order_flow_imbalance=usable_imbalance,
        policy=policy,
    )
    return PositionExitEvaluation(
        action=decision.action,
        quantity=decision.quantity,
        reason=decision.reason,
        r_multiple=decision.r_multiple,
        active_stop=decision.active_stop,
        peak_price=peak_price,
        current_price=current_price,
        quote_is_stale=quote_is_stale,
        momentum_reversal=momentum_reversal,
        order_flow_imbalance=usable_imbalance,
        order_flow_fresh=order_flow_fresh,
    )
