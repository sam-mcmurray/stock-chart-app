from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class RiskLimits:
    """Hard limits applied after a strategy proposes an entry and stop."""

    risk_per_trade_pct: float = 0.005
    daily_loss_limit_pct: float = 0.02
    max_aggregate_open_risk_pct: float = 0.015
    max_position_pct: float = 0.10
    max_volume_participation_pct: float = 0.005
    slippage_bps: float = 20.0


@dataclass(frozen=True)
class PositionSizeDecision:
    allowed: bool
    quantity: int
    reason: str | None
    risk_budget: float
    risk_per_share: float
    estimated_loss_at_stop: float
    position_notional: float
    caps: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExitPolicy:
    partial_profit_r: float = 1.5
    partial_fraction: float = 0.5
    break_even_r: float = 1.0
    runner_activation_r: float = 2.0
    runner_trail_r: float = 0.75
    order_flow_exit_threshold: float = -0.35


@dataclass(frozen=True)
class ExitDecision:
    action: str
    quantity: int
    reason: str | None
    r_multiple: float
    active_stop: float

    def to_dict(self) -> dict:
        return asdict(self)


def _finite(value: float | int | None, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def calculate_long_position_size(
    *,
    account_equity: float,
    buying_power: float,
    entry_price: float,
    stop_price: float,
    daily_realized_pnl: float = 0.0,
    aggregate_open_risk: float = 0.0,
    average_minute_volume: float | None = None,
    limits: RiskLimits | None = None,
) -> PositionSizeDecision:
    """Return the largest long position that satisfies every configured cap."""
    limits = limits or RiskLimits()
    equity = _finite(account_equity)
    power = max(_finite(buying_power), 0.0)
    entry = _finite(entry_price)
    stop = _finite(stop_price)

    empty = PositionSizeDecision(False, 0, None, 0.0, 0.0, 0.0, 0.0, {})
    if equity <= 0:
        return PositionSizeDecision(**{**asdict(empty), "reason": "invalid_account_equity"})
    if power <= 0:
        return PositionSizeDecision(**{**asdict(empty), "reason": "no_buying_power"})
    if entry <= 0 or stop <= 0 or stop >= entry:
        return PositionSizeDecision(**{**asdict(empty), "reason": "invalid_long_stop"})

    per_trade_budget = equity * max(_finite(limits.risk_per_trade_pct), 0.0)
    daily_limit = equity * max(_finite(limits.daily_loss_limit_pct), 0.0)
    daily_loss = max(-_finite(daily_realized_pnl), 0.0)
    daily_remaining = max(daily_limit - daily_loss, 0.0)
    aggregate_limit = equity * max(_finite(limits.max_aggregate_open_risk_pct), 0.0)
    aggregate_remaining = max(aggregate_limit - max(_finite(aggregate_open_risk), 0.0), 0.0)
    risk_budget = min(per_trade_budget, daily_remaining, aggregate_remaining)
    slippage_per_share = entry * max(_finite(limits.slippage_bps), 0.0) / 10_000
    risk_per_share = entry - stop + slippage_per_share

    if daily_remaining <= 0:
        return PositionSizeDecision(False, 0, "daily_loss_limit_reached", 0.0, risk_per_share, 0.0, 0.0, {})
    if aggregate_remaining <= 0:
        return PositionSizeDecision(False, 0, "aggregate_open_risk_limit_reached", 0.0, risk_per_share, 0.0, 0.0, {})
    if risk_budget <= 0 or risk_per_share <= 0:
        return PositionSizeDecision(False, 0, "no_risk_capacity", 0.0, risk_per_share, 0.0, 0.0, {})

    caps = {
        "risk": math.floor(risk_budget / risk_per_share),
        "buying_power": math.floor(power / entry),
        "position": math.floor((equity * max(_finite(limits.max_position_pct), 0.0)) / entry),
    }
    if average_minute_volume is not None:
        caps["liquidity"] = math.floor(
            max(_finite(average_minute_volume), 0.0)
            * max(_finite(limits.max_volume_participation_pct), 0.0)
        )

    quantity = min(caps.values()) if caps else 0
    if quantity < 1:
        limiting_cap = min(caps, key=caps.get) if caps else "risk"
        return PositionSizeDecision(
            False,
            0,
            f"{limiting_cap}_cap_below_one_share",
            risk_budget,
            risk_per_share,
            0.0,
            0.0,
            caps,
        )

    return PositionSizeDecision(
        True,
        quantity,
        None,
        risk_budget,
        risk_per_share,
        quantity * risk_per_share,
        quantity * entry,
        caps,
    )


def evaluate_long_exit(
    *,
    quantity: int,
    entry_price: float,
    hard_stop_price: float,
    current_price: float,
    peak_price: float,
    partial_taken: bool,
    active_stop_price: float | None = None,
    quote_is_stale: bool = False,
    trading_disabled: bool = False,
    momentum_reversal: bool = False,
    order_flow_imbalance: float | None = None,
    policy: ExitPolicy | None = None,
) -> ExitDecision:
    """Evaluate long exits in safety priority order; this function never places orders."""
    policy = policy or ExitPolicy()
    qty = max(math.floor(_finite(quantity)), 0)
    entry = _finite(entry_price)
    hard_stop = _finite(hard_stop_price)
    current = _finite(current_price)
    peak = max(_finite(peak_price), current)
    initial_risk = entry - hard_stop

    if qty <= 0 or entry <= 0 or hard_stop <= 0 or initial_risk <= 0 or current <= 0:
        return ExitDecision("blocked", 0, "invalid_position_state", 0.0, hard_stop)

    r_multiple = (current - entry) / initial_risk
    if trading_disabled:
        return ExitDecision("exit_all", qty, "kill_switch", r_multiple, hard_stop)
    if quote_is_stale:
        return ExitDecision("exit_all", qty, "market_data_stale", r_multiple, hard_stop)
    if current <= hard_stop:
        return ExitDecision("exit_all", qty, "hard_stop", r_multiple, hard_stop)

    active_stop = max(hard_stop, _finite(active_stop_price))
    if r_multiple >= policy.break_even_r:
        active_stop = max(active_stop, entry)
    if (peak - entry) / initial_risk >= policy.runner_activation_r:
        active_stop = max(active_stop, peak - policy.runner_trail_r * initial_risk)
    if current <= active_stop:
        return ExitDecision("exit_all", qty, "managed_stop", r_multiple, active_stop)

    imbalance = None if order_flow_imbalance is None else _finite(order_flow_imbalance)
    if momentum_reversal and imbalance is not None and imbalance <= policy.order_flow_exit_threshold:
        return ExitDecision("exit_all", qty, "confirmed_momentum_reversal", r_multiple, active_stop)

    if not partial_taken and r_multiple >= policy.partial_profit_r:
        partial_qty = max(1, min(qty, math.floor(qty * policy.partial_fraction)))
        return ExitDecision("take_partial", partial_qty, "partial_profit_target", r_multiple, active_stop)

    return ExitDecision("hold", 0, None, r_multiple, active_stop)
