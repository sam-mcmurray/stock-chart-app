"""Bounded synchronous shadow rollouts; no stream, broker or fill fabrication.

Each column is an independent simulated account, not a shared cash portfolio.
Equity must be marked after the preceding action's simulated execution costs.
"""
from datetime import datetime
import math
from zoneinfo import ZoneInfo

import torch

from drl_agent.live_recurrent import Rollout
from drl_agent.live_reward import RewardConfig, risk_adjusted_reward, session_bounds


class LiveRolloutCollector:
    def __init__(self, agent, symbols, *, capital=500.0, max_gap_seconds=3.0, reward_config=None):
        self.symbols = tuple(symbols)
        if (not self.symbols or len(set(self.symbols)) != len(self.symbols)
                or len(self.symbols) > agent.max_symbols
                or not all(isinstance(s, str) and s.strip() for s in self.symbols)
                or not math.isfinite(capital) or capital <= 0
                or not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0):
            raise ValueError('Invalid collector configuration')
        self.agent, self.capital, self.max_gap = agent, capital, max_gap_seconds
        self.reward_config = reward_config or RewardConfig()
        self.peak_equity = None
        self.discarded_transitions = 0
        self.reset('waiting_for_fresh_market')

    def reset(self, reason):
        self.discarded_transitions += len(getattr(self, 'rows', []))
        self.rows = []
        self.pending = None
        self.hidden = self.agent.policy.initial_state(len(self.symbols))
        self.version = self.agent.version
        self.status = reason

    def observe(self, *, symbols, timestamp, observations, masks, quote_times,
                book_times, equity, inventory_fraction, session_open):
        """Return sampled actions or a completed rollout, never both.

        session_open must come from the market-session controller, not quote
        activity. Weekend rejection is an additional guard, not a holiday calendar.
        Missing/stale frames discard the incomplete window rather than bridge it.
        """
        if tuple(symbols) != self.symbols:
            raise ValueError('Symbol columns changed within collector')
        if self.agent.version != self.version:
            self.reset('policy_changed')
        count = len(self.symbols)
        device = next(self.agent.policy.parameters()).device
        obs = torch.as_tensor(observations, dtype=torch.float32, device=device).detach().clone()
        mask = torch.as_tensor(masks, device=device).detach().clone()
        eq = torch.as_tensor(equity, dtype=torch.float32, device=device).detach().clone()
        inventory = torch.as_tensor(inventory_fraction, dtype=torch.float32, device=device).detach().clone()
        quotes = torch.as_tensor(quote_times, dtype=torch.float64)
        books = torch.as_tensor(book_times, dtype=torch.float64)
        if (obs.shape != (count, self.agent.policy.encoder[0].in_features)
                or mask.shape != (count, 4) or mask.dtype != torch.bool
                or eq.shape != (count,) or quotes.shape != (count,) or books.shape != (count,)
                or inventory.shape != (count,) or not torch.isfinite(inventory).all() or (inventory < 0).any()
                or not torch.isfinite(obs).all() or not torch.isfinite(eq).all()
                or not mask.any(dim=-1).all() or not math.isfinite(timestamp)):
            raise ValueError('Invalid live frame')
        date = datetime.fromtimestamp(timestamp, ZoneInfo('America/New_York')).date()
        if session_open is not True or date.weekday() >= 5:
            self.reset('market_closed')
            return None
        bounds = session_bounds(date)
        if bounds is None or not bounds[0] <= timestamp < bounds[1]:
            self.reset('market_closed')
            return None
        if (not torch.isfinite(quotes).all() or not torch.isfinite(books).all()
                or not ((timestamp - quotes >= 0) & (timestamp - quotes <= 3)).all()
                or not ((timestamp - books >= 0) & (timestamp - books <= 2)).all()):
            self.reset('stale_market')
            return None
        if self.pending is not None:
            elapsed = timestamp - self.pending['timestamp']
            if elapsed <= 0:
                self.status = 'waiting_for_next_timestamp'
                return None
            if elapsed > self.max_gap or date != self.pending['date']:
                self.reset('market_gap')
                return None
        start = torch.full((1, count), self.pending is None and not self.rows,
                           dtype=torch.bool, device=device)
        with torch.no_grad():
            distribution, values, hidden = self.agent.policy(obs.unsqueeze(0), mask.unsqueeze(0), start, self.hidden)
            values = values[0]
            if self.peak_equity is None:
                self.peak_equity = torch.maximum(eq, torch.full_like(eq, self.capital))
            if self.pending is not None:
                row = self.pending
                reward, self.peak_equity, parts = risk_adjusted_reward(
                    previous=row['equity'], current=eq, peak=self.peak_equity,
                    previous_inventory=row['inventory'], inventory=inventory, capital=self.capital,
                    start=row['timestamp'], end=timestamp, deadline=bounds[1] - 300,
                    config=self.reward_config)
                row.update(next_values=values.clone(),
                           rewards=reward, reward_components=parts,
                           seconds=torch.full_like(values, timestamp - row['timestamp']))
                self.rows.append(row)
            if len(self.rows) == self.agent.max_steps:
                result = self._rollout()
                components = {key: torch.stack([r['reward_components'][key] for r in self.rows])
                              for key in self.rows[0]['reward_components']}
                self.reset('rollout_ready')
                self.discarded_transitions -= self.agent.max_steps
                return {'rollout': result, 'reward_components': components}
            actions = distribution.sample()[0]
            self.pending = dict(observations=obs, masks=mask, starts=start[0], actions=actions,
                logprobs=distribution.log_prob(actions.unsqueeze(0))[0], values=values.clone(),
                timestamp=timestamp, date=date, equity=eq, inventory=inventory)
            self.hidden = tuple(h.detach() for h in hidden)
        self.status = 'collecting'
        return {'actions': actions.clone(), 'policy_version': self.version}

    def _rollout(self):
        def stack(name):
            return torch.stack([row[name] for row in self.rows])
        flags = torch.zeros((len(self.rows), len(self.symbols)), dtype=torch.bool,
                            device=self.rows[0]['observations'].device)
        truncated = flags.clone()
        truncated[-1] = True
        return Rollout(observations=stack('observations'), masks=stack('masks'),
            starts=stack('starts'), actions=stack('actions'), logprobs=stack('logprobs'),
            values=stack('values'), next_values=stack('next_values'), rewards=stack('rewards'),
            seconds=stack('seconds'), terminated=flags, truncated=truncated,
            initial_hidden=self.agent.policy.initial_state(len(self.symbols)), policy_version=self.version)
