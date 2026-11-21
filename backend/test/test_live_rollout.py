from datetime import datetime, timezone

import pytest
import torch

from drl_agent.live_recurrent import RecurrentPPO
from drl_agent.live_rollout import LiveRolloutCollector


FRIDAY = datetime(2026, 11, 20, 16, tzinfo=timezone.utc).timestamp()


def frame(timestamp=FRIDAY, **changes):
    result = dict(symbols=('ABC', 'DEF'), timestamp=timestamp,
        observations=[[.1] * 6, [.2] * 6], masks=[[True, True, False, False]] * 2,
        quote_times=[timestamp] * 2, book_times=[timestamp] * 2,
        equity=[500., 500.], inventory_fraction=[0., 0.], session_open=True)
    result.update(changes)
    return result


def test_completed_causal_rollout_updates_real_weights():
    agent = RecurrentPPO(6, max_steps=3)
    collector = LiveRolloutCollector(agent, ('ABC', 'DEF'))
    first = collector.observe(**frame())
    assert (first['actions'] < 2).all()
    assert not collector.rows
    collector.observe(**frame(FRIDAY + 1, equity=[499., 502.]))
    collector.observe(**frame(FRIDAY + 2, equity=[501., 501.]))
    result = collector.observe(**frame(FRIDAY + 3, equity=[500., 503.]))
    assert 'actions' not in result
    data = result['rollout']
    assert (data.rewards.sum(0) < torch.tensor([0., .006])).all()
    assert torch.allclose(result['reward_components']['net_return'].sum(0), torch.tensor([0., .006]))
    assert data.truncated[-1].all() and not data.terminated.any()
    assert data.starts[0].all() and not data.starts[1:].any()
    assert len(collector.rows) == 0 and collector.pending is None
    assert collector.discarded_transitions == 0
    before = [p.clone() for p in agent.policy.parameters()]
    update = agent.update(data, epochs=2)
    assert update['transitions'] == 6
    assert any(not torch.equal(a, b) for a, b in zip(before, agent.policy.parameters()))


@pytest.mark.parametrize('changes,status', [
    ({'session_open': False}, 'market_closed'),
    ({'book_times': [FRIDAY - 10] * 2}, 'stale_market'),
    ({'quote_times': [FRIDAY + 10] * 2}, 'stale_market'),
    ({'book_times': [float('nan')] * 2}, 'stale_market'),
])
def test_bad_market_never_produces_actions_or_rewards(changes, status):
    collector = LiveRolloutCollector(RecurrentPPO(6), ('ABC', 'DEF'))
    collector.observe(**frame())
    assert collector.observe(**frame(**changes)) is None
    assert collector.status == status
    assert collector.pending is None and collector.rows == []


def test_weekend_blocked_even_with_mistaken_open_flag():
    collector = LiveRolloutCollector(RecurrentPPO(6), ('ABC', 'DEF'))
    assert collector.observe(**frame(FRIDAY + 86400)) is None
    assert collector.status == 'market_closed'


def test_gap_and_duplicate_ticks_do_not_manufacture_experience():
    collector = LiveRolloutCollector(RecurrentPPO(6), ('ABC', 'DEF'))
    collector.observe(**frame())
    assert collector.observe(**frame()) is None
    assert not collector.rows
    assert collector.observe(**frame(FRIDAY + 10)) is None
    assert collector.status == 'market_gap' and collector.pending is None
    collector.observe(**frame(FRIDAY + 11))
    assert not collector.rows


def test_identity_and_frame_shape_guard():
    collector = LiveRolloutCollector(RecurrentPPO(6), ('ABC', 'DEF'))
    with pytest.raises(ValueError, match='columns'):
        collector.observe(**frame(symbols=('DEF', 'ABC')))
    with pytest.raises(ValueError, match='frame'):
        collector.observe(**frame(equity=[float('nan'), 500.]))


def test_updated_policy_discards_unfinished_old_rollout():
    agent = RecurrentPPO(6)
    collector = LiveRolloutCollector(agent, ('ABC', 'DEF'))
    collector.observe(**frame())
    agent.version += 1
    result = collector.observe(**frame(FRIDAY + 1))
    assert result['policy_version'] == 1
    assert not collector.rows and collector.pending['starts'].all()
