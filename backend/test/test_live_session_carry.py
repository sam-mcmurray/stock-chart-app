from dataclasses import replace
from datetime import datetime, timezone

import pytest

from drl_agent.live_checkpoint import restore_checkpoint, save_checkpoint
from drl_agent.live_execution import ExecutionQuote, LiveShadowExecution
from drl_agent.live_recurrent import RecurrentPPO
from drl_agent.live_reward import RewardConfig


def quote(timestamp, bid=5., ask=5.01):
    return ExecutionQuote('ABC', timestamp, timestamp, timestamp, timestamp, timestamp,
        bid, ask, 1000, 1000, 1000, 1000, 10000)


def held():
    sim = LiveShadowExecution('ABC', '2026-11-20')
    ts = datetime(2026, 11, 20, 16, tzinfo=timezone.utc).timestamp()
    sim.advance(quote(ts)); sim.propose(3); sim.advance(quote(ts + 1))
    return sim


def test_carry_position_costs_and_cash_then_observe_gap_once():
    sim = held()
    cash, qty, cost, power, equity = sim.cash, sim.quantity, sim.cost_basis, sim.buying_power, sim.equity
    sim.settle_session(sim.close)
    sim.start_session('2026-11-23')
    assert (sim.cash, sim.quantity, sim.cost_basis, sim.buying_power) == (cash, qty, cost, power)
    assert not sim.propose(3)
    unavailable = sim.advance(replace(quote(sim.open - 1), delayed=True))
    assert unavailable['equity'] is None and sim.carry_boundary is not None
    first = sim.advance(quote(sim.open, bid=4.5, ask=4.51))
    assert first['fill'] is None and first['pending_exit']
    outcome = first['overnight_outcome']
    assert outcome['net_return'] == pytest.approx((first['equity'] - equity) / 500)
    assert outcome['net_return'] < 0 and not outcome['training_attributed']
    second = sim.advance(quote(sim.open + 1, bid=4.5, ask=4.51))
    assert second['fill']['side'] == 'sell' and sim.quantity == 0
    assert second['overnight_outcome'] is None and sim.cash < 500


def test_no_quote_over_multiple_sessions_keeps_original_boundary():
    sim = held()
    original_equity = sim.equity
    sim.settle_session(sim.close); sim.start_session('2026-11-23')
    sim.settle_session(sim.close); sim.start_session('2026-11-24')
    outcome = sim.advance(quote(sim.open))['overnight_outcome']
    assert outcome['from_session'] == '2026-11-20'
    assert outcome['reference_equity'] == original_equity


def test_restart_preserves_unobserved_boundary(tmp_path):
    sim = held()
    sim.settle_session(sim.close); sim.start_session('2026-11-23')
    path = tmp_path / 'carry.pt'
    save_checkpoint(path, RecurrentPPO(2), [sim], feature_names=['a', 'b'],
        reward_config=RewardConfig(), peak_equity=[500])
    restored = restore_checkpoint(path, feature_names=['a', 'b'], reward_config=RewardConfig())['executions'][0]
    assert restored.carry_boundary == sim.carry_boundary
    assert restored.advance(quote(restored.open))['overnight_outcome'] is not None
    assert restored.advance(quote(restored.open + 1))['overnight_outcome'] is None


def test_invalid_rollover_leaves_ledger_untouched_and_flat_cash_not_reset():
    sim = held()
    with pytest.raises(ValueError, match='Settle'):
        sim.start_session('2026-11-23')
    sim.propose(0); sim.advance(quote(sim.latest.timestamp + 1))
    assert sim.quantity == 0 and sim.cash < 500
    cash = sim.cash
    sim.settle_session(sim.close)
    for day in ('2026-11-20', '2026-11-21', '2026-11-26'):
        with pytest.raises(ValueError):
            sim.start_session(day)
        assert sim.session_date == '2026-11-20' and sim.cash == cash
    sim.start_session('2026-11-23')
    assert sim.cash == cash and sim.carry_boundary is None
    assert sim.daily_realized_baseline == sim.realized
