from dataclasses import replace
from datetime import datetime, timezone

import pytest
import torch

from drl_agent.live_execution import ExecutionQuote, LiveShadowExecution
from drl_agent.live_recurrent import RecurrentPPO
from drl_agent.live_rollout import LiveRolloutCollector

START = datetime(2026, 11, 20, 16, tzinfo=timezone.utc).timestamp()


def quote(offset=0, **changes):
    ts = START + offset
    return replace(ExecutionQuote('ABC', ts, ts, ts, ts, ts,
        5., 5.01, 1000, 1000, 1000, 1000, 10000), **changes)


def engine():
    return LiveShadowExecution('ABC', '2026-11-20')


def test_later_fill_spread_fees_and_risk_cap():
    sim = engine()
    assert sim.advance(quote())['equity'] == 500
    sim.propose(3)
    assert sim.advance(quote())['fill'] is None
    result = sim.advance(quote(1))
    assert result['fill']['side'] == 'buy'
    assert result['fill']['price'] > 5.01
    assert 0 < sim.quantity * sim.entry <= 50
    assert result['equity'] < 500 and result['fees'] > 0
    assert result['broker_calls'] == 0


def test_displayed_depth_caps_partial_fill_and_no_recycled_cash():
    sim = engine()
    sim.advance(quote()); sim.propose(3)
    result = sim.advance(quote(1, book_ask_shares=8))
    assert result['fill']['quantity'] == 2 and result['reason'] == 'partial_fill'
    power = sim.buying_power
    sim.propose(0)
    result = sim.advance(quote(2, book_bid_shares=4))
    assert result['fill']['quantity'] == 1 and result['pending_exit']
    assert not sim.propose(3)
    assert sim.advance(quote(3))['fill']['quantity'] == 1
    assert sim.quantity == 0 and sim.buying_power == pytest.approx(power)
    assert sim.cash < 500


def test_stale_and_reused_quote_cannot_fill():
    sim = engine()
    sim.advance(quote()); sim.propose(3)
    assert sim.advance(quote(1, event_ts=START))['fill'] is None
    assert sim.advance(quote(2, book_event_ts=START - 10))['equity'] is None
    assert sim.pending is None and sim.quantity == 0
    assert sim.advance(quote(3))['fill'] is None


def test_stop_intent_waits_for_next_quote_and_survives_illiquidity():
    sim = engine()
    sim.advance(quote()); sim.propose(3); sim.advance(quote(1))
    result = sim.advance(quote(2, bid=4.8, ask=4.81))
    assert result['fill'] is None and result['pending_exit']
    result = sim.advance(quote(3, bid=4.8, ask=4.81, book_bid_shares=0))
    assert result['fill'] is None and result['pending_exit']
    result = sim.advance(quote(4, bid=4.8, ask=4.81))
    assert result['fill']['side'] == 'sell' and sim.quantity == 0
    assert sim.realized < 0


def test_calendar_and_bad_input_guards():
    with pytest.raises(ValueError, match='session'):
        LiveShadowExecution('ABC', '2026-11-21')
    sim = engine()
    assert sim.advance(quote(86400))['equity'] is None
    assert not sim.propose(3)
    with pytest.raises(ValueError):
        engine().advance(quote(timestamp=float('nan')))
    with pytest.raises(ValueError):
        engine().advance(quote(book_ask_shares=-1))


def test_execution_rewards_reach_ppo_without_broker():
    torch.manual_seed(7)
    sim = engine()
    agent = RecurrentPPO(6, max_steps=3)
    collector = LiveRolloutCollector(agent, ('ABC',))
    for offset in range(4):
        q = quote(offset)
        event = sim.advance(q)
        result = collector.observe(symbols=('ABC',), timestamp=q.timestamp,
            observations=[[.1] * 6], masks=[[True] * 4],
            quote_times=[q.event_ts], book_times=[q.book_event_ts],
            equity=[event['equity']], inventory_fraction=[sim.quantity * q.bid / 500], session_open=True)
        if 'actions' in result:
            sim.propose(int(result['actions'][0]))
    data = result['rollout']
    assert result['reward_components']['net_return'].sum().item() == pytest.approx((sim.equity - 500) / 500, abs=1e-7)
    assert data.rewards.sum().item() <= (sim.equity - 500) / 500 + 1e-7
    assert agent.update(data, epochs=2)['transitions'] == 3


def test_close_deadline_cancels_entries_and_persists_flattening():
    sim = engine()
    deadline = sim.close - 300 - START
    sim.advance(quote(deadline - 1)); sim.propose(3)
    assert sim.advance(quote(deadline))['fill'] is None
    assert not sim.propose(3)
    sim = engine()
    sim.advance(quote()); sim.propose(3); sim.advance(quote(1))
    assert sim.advance(quote(deadline))['pending_exit']
    assert sim.pending['reason'] == 'end_of_day_flat'
    assert sim.advance(quote(deadline + 1, book_bid_shares=0))['pending_exit']
    result = sim.advance(quote(deadline + 2))
    assert result['fill']['side'] == 'sell' and sim.quantity == 0


def test_spread_and_slippage_reported_only_for_executed_shares():
    narrow, wide = engine(), engine()
    results = []
    for sim, ask in ((narrow, 5.01), (wide, 5.04)):
        sim.advance(quote(ask=ask)); sim.propose(3)
        result = sim.advance(quote(1, ask=ask, book_ask_shares=8))
        assert result['fill']['quantity'] == 2
        assert result['costs']['spread'] == pytest.approx(ask - 5.)
        assert result['costs']['slippage'] > 0 and result['costs']['fees'] > 0
        results.append(result)
    assert results[1]['equity'] < results[0]['equity']
    assert narrow.advance(quote(2))['costs'] == dict(spread=0., slippage=0., fees=0.)
