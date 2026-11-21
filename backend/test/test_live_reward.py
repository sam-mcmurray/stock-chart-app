from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import torch

from drl_agent.live_reward import RewardConfig, risk_adjusted_reward, session_bounds


def reward(previous=500., current=500., inventory=.1, start=0, end=1, deadline=100):
    return risk_adjusted_reward(previous=torch.tensor([previous]), current=torch.tensor([current]),
        peak=torch.tensor([500.]), previous_inventory=torch.tensor([inventory]),
        inventory=torch.tensor([inventory]), capital=500., start=start, end=end, deadline=deadline,
        config=RewardConfig())


def test_risk_penalties_are_quadratic_and_do_not_reward_recovery():
    _, _, small = reward(current=495)
    _, _, large = reward(current=490)
    assert large['drawdown_penalty'].item() == pytest.approx(4 * small['drawdown_penalty'].item())
    _, _, recovered = reward(previous=490, current=495)
    assert recovered['drawdown_penalty'].item() == 0
    assert reward(inventory=.2)[2]['inventory_penalty'].item() == pytest.approx(4 * reward()[2]['inventory_penalty'].item())


def test_flat_reward_zero_and_costs_not_double_charged():
    assert reward(inventory=0)[0].item() == 0
    result, _, parts = reward(current=499, inventory=0)
    assert parts['net_return'].item() == pytest.approx(-1 / 500)
    assert result.item() == pytest.approx(-1 / 500 - (1 / 500) ** 2)


def test_deadline_overlap_and_elapsed_time():
    assert reward(start=98, end=100)[2]['late_inventory_penalty'].item() == 0
    one = reward(start=99, end=101)[2]['late_inventory_penalty'].item()
    two = reward(start=100, end=102)[2]['late_inventory_penalty'].item()
    assert two == pytest.approx(2 * one) and one > 0
    assert reward(inventory=0, start=100, end=102)[2]['late_inventory_penalty'].item() == 0


def test_regular_early_close_dst_and_holiday():
    for day, hour in ((date(2026, 11, 20), 15), (date(2026, 11, 27), 12), (date(2026, 7, 1), 15)):
        deadline = datetime.fromtimestamp(session_bounds(day)[1] - 300, ZoneInfo('America/New_York'))
        assert (deadline.hour, deadline.minute) == (hour, 55)
    assert session_bounds(date(2026, 11, 26)) is None
    with pytest.raises(ValueError):
        RewardConfig(drawdown_weight=-1)
