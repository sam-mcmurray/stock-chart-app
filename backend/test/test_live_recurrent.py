import pytest
import torch

from drl_agent.live_recurrent import RecurrentPPO, Rollout, advantages


def rollout(agent, steps=8, batch=2):
    observations = torch.randn(steps, batch, 6)
    masks = torch.ones(steps, batch, 4, dtype=torch.bool)
    masks[:, :, 3] = False
    starts = torch.zeros(steps, batch, dtype=torch.bool)
    starts[0] = True
    hidden = agent.policy.initial_state(batch)
    with torch.no_grad():
        dist, values, _ = agent.policy(observations, masks, starts, hidden)
        actions = dist.sample()
    return Rollout(observations, masks, starts, actions, dist.log_prob(actions).detach(),
        values, torch.zeros_like(values), torch.ones_like(values) * .01,
        torch.ones_like(values), torch.zeros_like(starts), torch.zeros_like(starts), hidden, agent.version)


def test_actual_update_is_finite_bounded_and_not_replayed():
    torch.manual_seed(2)
    agent = RecurrentPPO(6)
    data = rollout(agent)
    before = {k: v.clone() for k, v in agent.policy.state_dict().items()}
    result = agent.update(data, epochs=2)
    assert result['transitions'] == 16 and result['policy_version'] == 1
    assert any(not torch.equal(before[k], v) for k, v in agent.policy.state_dict().items())
    assert all(torch.isfinite(p).all() for p in agent.policy.parameters())
    with pytest.raises(ValueError, match='consumed'):
        agent.update(data)


def test_temporal_memory_resets_per_symbol_and_actions_are_masked():
    agent = RecurrentPPO(6)
    data = rollout(agent)
    dist, _, _ = agent.policy(data.observations, data.masks, data.starts)
    assert (dist.probs[:, :, 3] == 0).all()
    # First symbol starts a new episode: its result cannot depend on older history.
    starts = data.starts.clone()
    starts[4, 0] = True
    first = agent.policy(data.observations, data.masks, starts)[0].probs
    changed = data.observations.clone()
    changed[:4, 0] += 100
    second = agent.policy(changed, data.masks, starts)[0].probs
    assert torch.allclose(first[4:, 0], second[4:, 0])
    assert torch.allclose(first[:, 1], second[:, 1])


def test_bootstrap_truncation_but_not_terminal_and_elapsed_discount():
    agent = RecurrentPPO(6)
    data = rollout(agent, steps=2, batch=1)
    data.values.zero_(); data.rewards.zero_(); data.next_values.fill_(10)
    data.terminated[0] = True; data.truncated[1] = True
    data.seconds[1] = 2
    advantage, _ = advantages(data, gamma=.5)
    assert advantage[0].item() == 0
    assert advantage[1].item() == 2.5


def test_refuses_unfinished_rewards_oversized_and_broken_boundaries():
    agent = RecurrentPPO(6, max_steps=4)
    with pytest.raises(ValueError, match='capacity'):
        agent.update(rollout(agent, steps=5))
    data = rollout(agent, steps=4)
    data.rewards[0] = float('nan')
    with pytest.raises(ValueError, match='nonfinite'):
        agent.update(data)
    data.rewards.zero_(); data.truncated[1] = True
    with pytest.raises(ValueError, match='boundary'):
        agent.update(data)


def test_refuses_mixed_policy_rollout_before_changing_weights():
    agent = RecurrentPPO(6)
    data = rollout(agent)
    data.logprobs[0] += .1
    before = {k: v.clone() for k, v in agent.policy.state_dict().items()}
    with pytest.raises(ValueError, match='frozen collection policy'):
        agent.update(data)
    assert agent.version == 0
    assert all(torch.equal(before[k], v) for k, v in agent.policy.state_dict().items())
