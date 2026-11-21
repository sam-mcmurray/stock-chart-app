from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class StopLossDecision:
    triggered: bool
    reason: str | None
    trigger_type: str | None
    loss_amount: float
    loss_pct: float
    max_loss_amount: float
    stop_price: float
    peak_price: float
    trailing_stop_price: float
    trailing_drawdown_amount: float
    trailing_drawdown_pct: float

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_stop_loss(
    *,
    quantity: float,
    average_price: float,
    current_price: float,
    max_loss_pct: float,
    max_loss_amount: float | None = None,
    peak_price: float | None = None,
    trailing_loss_pct: float | None = None,
    trailing_loss_amount: float | None = None,
) -> StopLossDecision:
    """
    Hard long-position stop loss with optional trailing/sliding stop.
    If breached, callers must force an exit.
    """
    def _finite(value, default=0.0) -> float:
        try:
            number = float(value or default)
        except (TypeError, ValueError):
            return float(default)
        return number if math.isfinite(number) else float(default)

    qty = max(_finite(quantity), 0.0)
    avg = max(_finite(average_price), 0.0)
    current = max(_finite(current_price), 0.0)
    pct = max(_finite(max_loss_pct), 0.0)
    explicit_amount = None if max_loss_amount is None else max(_finite(max_loss_amount), 0.0)
    peak = max(_finite(peak_price), avg, current)
    trail_pct = pct if trailing_loss_pct is None else max(_finite(trailing_loss_pct), 0.0)
    explicit_trail_amount = None if trailing_loss_amount is None else max(_finite(trailing_loss_amount), 0.0)

    if qty <= 0 or avg <= 0 or current <= 0:
        stop_price = avg * (1.0 - pct) if avg > 0 else 0.0
        trailing_stop_price = peak * (1.0 - trail_pct) if peak > 0 else 0.0
        return StopLossDecision(
            False,
            None,
            None,
            0.0,
            0.0,
            explicit_amount or 0.0,
            stop_price,
            peak,
            trailing_stop_price,
            0.0,
            0.0,
        )

    position_value = qty * avg
    allowed_loss = explicit_amount if explicit_amount and explicit_amount > 0 else position_value * pct
    stop_price = max(avg - (allowed_loss / qty), 0.0)
    loss_amount = max((avg - current) * qty, 0.0)
    loss_pct = loss_amount / position_value if position_value > 0 else 0.0
    trailing_allowed_loss = (
        explicit_trail_amount
        if explicit_trail_amount and explicit_trail_amount > 0
        else peak * qty * trail_pct
    )
    trailing_stop_price = max(peak - (trailing_allowed_loss / qty), 0.0) if qty > 0 else 0.0
    trailing_drawdown_amount = max((peak - current) * qty, 0.0)
    trailing_drawdown_pct = (peak - current) / peak if peak > 0 and current < peak else 0.0

    if trailing_allowed_loss > 0 and current <= trailing_stop_price and peak > avg:
        return StopLossDecision(
            True,
            f"trailing_stop({current:.2f}<={trailing_stop_price:.2f})",
            "trailing",
            loss_amount,
            loss_pct,
            allowed_loss,
            stop_price,
            peak,
            trailing_stop_price,
            trailing_drawdown_amount,
            trailing_drawdown_pct,
        )

    if allowed_loss > 0 and loss_amount >= allowed_loss:
        return StopLossDecision(
            True,
            f"stop_loss({loss_amount:.2f}>={allowed_loss:.2f})",
            "fixed",
            loss_amount,
            loss_pct,
            allowed_loss,
            stop_price,
            peak,
            trailing_stop_price,
            trailing_drawdown_amount,
            trailing_drawdown_pct,
        )

    return StopLossDecision(
        False,
        None,
        None,
        loss_amount,
        loss_pct,
        allowed_loss,
        stop_price,
        peak,
        trailing_stop_price,
        trailing_drawdown_amount,
        trailing_drawdown_pct,
    )
