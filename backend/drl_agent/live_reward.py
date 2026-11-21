"""Risk-adjusted net-equity rewards; execution costs are already in equity."""
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
import math

import exchange_calendars as xcals
import torch


@lru_cache(maxsize=32)
def session_bounds(day):
    calendar = xcals.get_calendar('XNYS', start=day - timedelta(days=7), end=day + timedelta(days=7))
    if not calendar.is_session(day.isoformat()):
        return None
    return (calendar.session_open(day.isoformat()).timestamp(),
            calendar.session_close(day.isoformat()).timestamp())


@dataclass(frozen=True)
class RewardConfig:
    drawdown_weight: float = 1.0
    inventory_per_second: float = 0.000001
    late_inventory_per_second: float = 0.001

    def __post_init__(self):
        if any(not math.isfinite(v) or v < 0 for v in vars(self).values()):
            raise ValueError('Invalid reward weights')


def risk_adjusted_reward(*, previous, current, peak, previous_inventory, inventory,
                         capital, start, end, deadline, config):
    """Penalize increased squared drawdown and time-weighted squared exposure.

    Exposure is position notional / initial capital. Endpoint trapezoidal exposure
    approximates holding within a transition; no unseen intratick path is invented.
    Penalty coefficients are starting settings, not empirically tuned parameters.
    """
    peak_before = torch.maximum(peak, previous)
    peak_after = torch.maximum(peak_before, current)
    drawdown_before = ((peak_before - previous) / capital).clamp(min=0)
    drawdown_after = ((peak_after - current) / capital).clamp(min=0)
    exposure = (previous_inventory.square() + inventory.square()) / 2
    net = (current - previous) / capital
    drawdown = config.drawdown_weight * (drawdown_after.square() - drawdown_before.square()).clamp(min=0)
    holding = config.inventory_per_second * (end - start) * exposure
    late = config.late_inventory_per_second * max(0, end - max(start, deadline)) * exposure
    parts = dict(net_return=net, drawdown_penalty=drawdown, inventory_penalty=holding,
                 late_inventory_penalty=late)
    return net - drawdown - holding - late, peak_after, parts
