"""Small recurrent PPO research core. No broker, stream, or automatic deployment."""
from dataclasses import dataclass

import torch
from torch import nn
from torch.distributions import Categorical

VERSION = 'live-recurrent-ppo-v1'
TARGET_EXPOSURES = (0.0, 0.25, 0.5, 1.0)


class RecurrentPolicy(nn.Module):
    def __init__(self, input_dim, hidden_size=128):
        super().__init__()
        self.hidden_size = hidden_size
        self.encoder = nn.Sequential(nn.Linear(input_dim, 64), nn.Tanh())
        self.memory = nn.LSTM(64, hidden_size)
        self.actor = nn.Linear(hidden_size, len(TARGET_EXPOSURES))
        self.critic = nn.Linear(hidden_size, 1)

    def initial_state(self, batch=1):
        parameter = next(self.parameters())
        return tuple(parameter.new_zeros(1, batch, self.hidden_size) for _ in range(2))

    def forward(self, observations, masks, starts, hidden=None):
        if (observations.ndim != 3 or masks.shape != (*observations.shape[:2], 4)
                or starts.shape != observations.shape[:2] or masks.dtype != torch.bool
                or starts.dtype != torch.bool or not masks.any(dim=-1).all()
                or not torch.isfinite(observations).all()):
            raise ValueError('Invalid recurrent observations, masks or episode boundaries')
        hidden = self.initial_state(observations.shape[1]) if hidden is None else hidden
        outputs = []
        for index, row in enumerate(self.encoder(observations)):
            keep = (~starts[index]).to(row.dtype).view(1, -1, 1)
            hidden = tuple(value * keep for value in hidden)
            output, hidden = self.memory(row.unsqueeze(0), hidden)
            outputs.append(output)
        encoded = torch.cat(outputs)
        logits = self.actor(encoded).masked_fill(~masks, -torch.inf)
        return Categorical(logits=logits), self.critic(encoded).squeeze(-1), hidden


@dataclass
class Rollout:
    observations: torch.Tensor
    masks: torch.Tensor
    starts: torch.Tensor
    actions: torch.Tensor
    logprobs: torch.Tensor
    values: torch.Tensor
    next_values: torch.Tensor
    rewards: torch.Tensor
    seconds: torch.Tensor
    terminated: torch.Tensor
    truncated: torch.Tensor
    initial_hidden: tuple
    policy_version: int


def advantages(rollout, gamma=.999, gae_lambda=.95):
    # Bootstrap interrupted windows, but do not propagate credit into another episode.
    discounts = gamma ** rollout.seconds
    deltas = rollout.rewards + discounts * (~rollout.terminated) * rollout.next_values - rollout.values
    result = torch.zeros_like(deltas)
    carry = torch.zeros_like(deltas[0])
    for index in reversed(range(len(deltas))):
        continuation = ~(rollout.terminated[index] | rollout.truncated[index])
        carry = deltas[index] + discounts[index] * gae_lambda ** rollout.seconds[index] * continuation * carry
        result[index] = carry
    return result, result + rollout.values


class RecurrentPPO:
    def __init__(self, input_dim, *, hidden_size=128, max_steps=256, max_symbols=50):
        if not 1 <= max_steps <= 4096 or not 1 <= max_symbols <= 200:
            raise ValueError('Invalid rollout capacity')
        self.policy = RecurrentPolicy(input_dim, hidden_size)
        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=3e-4)
        self.version = 0
        self.max_steps, self.max_symbols = max_steps, max_symbols

    def update(self, rollout, *, epochs=4):
        if rollout.policy_version != self.version:
            raise ValueError('Rollout policy version mismatch or already consumed')
        if rollout.observations.ndim != 3:
            raise ValueError('Require time x independent symbol trajectories x features')
        shape = rollout.observations.shape[:2]
        if not 1 <= shape[0] <= self.max_steps or not 1 <= shape[1] <= self.max_symbols or not 1 <= epochs <= 10:
            raise ValueError('Rollout exceeds capacity')
        for name in ('actions', 'logprobs', 'values', 'next_values', 'rewards', 'seconds', 'starts', 'terminated', 'truncated'):
            value = getattr(rollout, name)
            if value.shape != shape or not torch.isfinite(value).all():
                raise ValueError('Incomplete or nonfinite rollout')
        if any(getattr(rollout, name).dtype != torch.bool for name in ('starts', 'terminated', 'truncated')):
            raise ValueError('Episode flags must be boolean')
        if (rollout.actions.dtype != torch.long or (rollout.actions < 0).any()
                or (rollout.actions >= 4).any() or (rollout.seconds <= 0).any()):
            raise ValueError('Invalid action or elapsed time')
        done = rollout.terminated | rollout.truncated
        if not torch.equal(rollout.starts[1:], done[:-1]):
            raise ValueError('Missing or unexpected episode boundary')
        if rollout.masks.shape != (*shape, 4) or rollout.masks.dtype != torch.bool:
            raise ValueError('Invalid masks')
        if not rollout.masks.gather(-1, rollout.actions.unsqueeze(-1)).all():
            raise ValueError('Recorded action was prohibited')
        if any(h.shape != (1, shape[1], self.policy.hidden_size) or not torch.isfinite(h).all()
               for h in rollout.initial_hidden) or len(rollout.initial_hidden) != 2:
            raise ValueError('Invalid initial memory')
        with torch.no_grad():
            original, original_values, _ = self.policy(rollout.observations, rollout.masks,
                rollout.starts, rollout.initial_hidden)
            if (not torch.allclose(original.log_prob(rollout.actions), rollout.logprobs, atol=1e-5, rtol=1e-5)
                    or not torch.allclose(original_values, rollout.values, atol=1e-5, rtol=1e-5)):
                raise ValueError('Rollout does not match the frozen collection policy')
            advantage, returns = advantages(rollout)
            if advantage.numel() > 1:
                advantage = (advantage - advantage.mean()) / (advantage.std(unbiased=False) + 1e-8)
        old_logprobs = rollout.logprobs.detach()
        for _ in range(epochs):
            distribution, values, _ = self.policy(rollout.observations.detach(), rollout.masks,
                rollout.starts, tuple(h.detach() for h in rollout.initial_hidden))
            logprobs = distribution.log_prob(rollout.actions)
            ratio = (logprobs - old_logprobs).exp()
            surrogate = torch.minimum(ratio * advantage, ratio.clamp(.8, 1.2) * advantage)
            loss = -surrogate.mean() + .5 * (values - returns).square().mean() - .01 * distribution.entropy().mean()
            if not torch.isfinite(loss):
                raise ValueError('Nonfinite PPO loss')
            self.optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(self.policy.parameters(), .5, error_if_nonfinite=True)
            self.optimizer.step()
        self.version += 1
        return dict(policy_version=self.version, transitions=shape[0] * shape[1], loss=float(loss.detach()),
                    training_scope='fixture_or_supplied_rollout', broker_calls=0)
