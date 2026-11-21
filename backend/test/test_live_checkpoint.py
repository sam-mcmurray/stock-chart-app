from datetime import datetime, timezone

import pytest
import torch

from drl_agent.live_checkpoint import restore_checkpoint, save_checkpoint
from drl_agent.live_execution import ExecutionQuote, LiveShadowExecution
from drl_agent.live_recurrent import RecurrentPPO, Rollout
from drl_agent.live_reward import RewardConfig

NAMES = [f'fixture_{i}' for i in range(6)]
START = datetime(2026, 11, 20, 16, tzinfo=timezone.utc).timestamp()


def quote(offset):
    ts = START + offset
    return ExecutionQuote('ABC', ts, ts, ts, ts, ts, 5., 5.01, 1000, 1000, 1000, 1000, 10000)


def trained_agent():
    agent = RecurrentPPO(6, max_steps=2)
    obs = torch.randn(2, 1, 6)
    masks = torch.ones(2, 1, 4, dtype=torch.bool)
    starts = torch.tensor([[True], [False]])
    hidden = agent.policy.initial_state()
    with torch.no_grad():
        dist, values, _ = agent.policy(obs, masks, starts, hidden)
        actions = dist.sample()
    data = Rollout(obs, masks, starts, actions, dist.log_prob(actions), values,
        torch.zeros_like(values), torch.ones_like(values) * .001, torch.ones_like(values),
        torch.zeros_like(starts), torch.tensor([[False], [True]]), hidden, agent.version)
    agent.update(data, epochs=1)
    return agent, data


def save(path, agent, sim):
    save_checkpoint(path, agent, [sim], feature_names=NAMES,
        reward_config=RewardConfig(), peak_equity=[500.])


def restore(path, **changes):
    args = dict(feature_names=NAMES, reward_config=RewardConfig())
    args.update(changes)
    return restore_checkpoint(path, **args)


def test_restore_weights_optimizer_rng_and_positions_without_old_rollouts(tmp_path):
    agent, old = trained_agent()
    sim = LiveShadowExecution('ABC', '2026-11-20')
    sim.advance(quote(0)); sim.propose(3); sim.advance(quote(1))
    path = tmp_path / 'state.pt'
    save(path, agent, sim)
    expected_random = torch.rand(5)
    result = restore(path)
    recovered, account = result['agent'], result['executions'][0]
    assert torch.equal(torch.rand(5), expected_random)
    assert all(torch.equal(v, recovered.policy.state_dict()[k]) for k, v in agent.policy.state_dict().items())
    for key, state in agent.optimizer.state_dict()['state'].items():
        for field, value in state.items():
            assert torch.equal(value, recovered.optimizer.state_dict()['state'][key][field])
    assert account.quantity == sim.quantity and account.cash == sim.cash
    assert account.pending['reason'] == 'restart_flatten' and account.latest is None
    assert not account.propose(3)
    assert not result['rollout_restored']
    with pytest.raises(ValueError, match='consumed'):
        recovered.update(old)
    assert account.advance(quote(2))['fill']['side'] == 'sell'


def test_failed_atomic_write_keeps_previous_checkpoint(tmp_path, monkeypatch):
    import drl_agent.live_checkpoint as checkpoint
    agent, _ = trained_agent()
    sim = LiveShadowExecution('ABC', '2026-11-20')
    path = tmp_path / 'state.pt'
    save(path, agent, sim)
    original = path.read_bytes()
    def failed_replace(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(checkpoint.os, 'replace', failed_replace)
    with pytest.raises(OSError):
        save(path, agent, sim)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))
    assert restore(path)['agent'].version == agent.version


def test_schema_reward_and_corrupt_ledger_rejected_without_rng_change(tmp_path):
    agent, _ = trained_agent()
    sim = LiveShadowExecution('ABC', '2026-11-20')
    path = tmp_path / 'state.pt'
    save(path, agent, sim)
    with pytest.raises(ValueError, match='Incompatible'):
        restore(path, feature_names=list(reversed(NAMES)))
    with pytest.raises(ValueError, match='Incompatible'):
        restore(path, reward_config=RewardConfig(drawdown_weight=2))
    saved = torch.load(path, weights_only=True)
    saved['executions'][0]['account']['cash'] += 1
    torch.save(saved, path)
    rng = torch.get_rng_state().clone()
    with pytest.raises(ValueError, match='ledger'):
        restore(path)
    assert torch.equal(rng, torch.get_rng_state())


def test_session_close_is_idempotent_preserves_unfilled_position(tmp_path):
    agent, _ = trained_agent()
    sim = LiveShadowExecution('ABC', '2026-11-20')
    sim.advance(quote(0)); sim.propose(3); sim.advance(quote(1))
    quantity, cash = sim.quantity, sim.cash
    with pytest.raises(ValueError, match='not closed'):
        sim.settle_session(sim.close - 1)
    report = sim.settle_session(sim.close)
    assert report['status'] == 'unresolved_position' and report['fill'] is None
    assert sim.settle_session(sim.close + 86400) == report
    assert sim.quantity == quantity and sim.cash == cash
    assert not sim.propose(3)
    assert sim.advance(quote(2))['reason'] == 'session_settled'
    path = tmp_path / 'closed.pt'
    save(path, agent, sim)
    recovered = restore(path)['executions'][0]
    assert recovered.session_report == report and recovered.quantity == quantity


def test_flat_session_report_does_not_invent_loss():
    sim = LiveShadowExecution('ABC', '2026-11-20')
    report = sim.settle_session(sim.close)
    assert report['status'] == 'flat' and report['cash'] == 500
    assert report['realized_pnl'] == 0 and not report['valuation_stale']
