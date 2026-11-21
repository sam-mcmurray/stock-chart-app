from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any


@dataclass(frozen=True)
class OrderFlowLimits:
    max_book_age_seconds: float = 2.0
    max_receive_age_seconds: float = 2.0
    min_combined_depth: int = 1_000
    min_imbalance: float = -0.35


@dataclass(frozen=True)
class OrderFlowDecision:
    allowed: bool
    reasons: list[str]
    service: str | None
    imbalance: float | None
    bid_depth: int
    ask_depth: int
    market_age_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def evaluate_order_flow_confirmation(
    snapshot: dict[str, Any], *, limits: OrderFlowLimits | None = None
) -> OrderFlowDecision:
    limits = limits or OrderFlowLimits()
    metrics = snapshot.get("book_metrics") or {}
    reasons: list[str] = []
    service = metrics.get("service")
    market_age = _finite_number(metrics.get("market_age_seconds"))
    receive_age = _finite_number(metrics.get("receive_age_seconds"))
    best_bid = _finite_number(metrics.get("best_bid"))
    best_ask = _finite_number(metrics.get("best_ask"))
    bid = _finite_number(metrics.get("bid_depth"))
    ask = _finite_number(metrics.get("ask_depth"))
    valid_depth = all(v is not None and v >= 0 and v.is_integer() for v in (bid, ask))
    bid_depth = int(bid) if valid_depth else 0
    ask_depth = int(ask) if valid_depth else 0
    imbalance = _finite_number(metrics.get("imbalance"))

    if service not in {"NASDAQ_BOOK", "NYSE_BOOK"}:
        reasons.append("level2_missing")
    if market_age is None or not 0 <= market_age <= limits.max_book_age_seconds:
        reasons.append("level2_market_stale")
    if receive_age is None or not 0 <= receive_age <= limits.max_receive_age_seconds:
        reasons.append("level2_receive_stale")
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        reasons.append("level2_invalid_top")
    if not valid_depth:
        reasons.append("level2_invalid_depth")
    if bid_depth + ask_depth < limits.min_combined_depth:
        reasons.append("level2_depth_too_low")
    if imbalance is None or not -1 <= imbalance <= 1 or imbalance < limits.min_imbalance:
        reasons.append("level2_ask_imbalance")

    return OrderFlowDecision(
        allowed=not reasons,
        reasons=reasons,
        service=service,
        imbalance=float(imbalance) if imbalance is not None else None,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        market_age_seconds=float(market_age) if market_age is not None else None,
    )
