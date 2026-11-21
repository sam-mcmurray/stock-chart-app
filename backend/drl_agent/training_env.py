"""Gymnasium adapter for the offline simulator; no broker or model loading."""
from datetime import datetime
import math
from zoneinfo import ZoneInfo

import gymnasium as gym
from gymnasium import spaces
import numpy as np

from drl_agent.simulator import Action, SimulationConfig, TradingSimulator

VERSION = "drl-training-env-v1"
PORTFOLIO_NAMES = (
    "cash", "equity_return", "exposure", "realized_pnl", "unrealized_pnl",
    "peak_equity_return", "maximum_drawdown", "open_risk", "daily_risk_remaining",
    "aggregate_risk_remaining", "position_present", "partial_taken", "pending_sale_fraction",
    "holding_hours", "stop_distance", "entry_return", "peak_return", "mark_present", "mark_stale",
    "clock_sin", "clock_cos", "weekday_sin", "weekday_cos",
)


def _bounded(value):
    """Fixed invertible scaling for ratios; no statistics fitted to future data."""
    return value / (1 + abs(value))


class NewsTradingEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, dataset: dict, *, config: SimulationConfig | None = None):
        super().__init__()
        self.simulator = TradingSimulator(dataset, config=config)
        self.source_digest = dataset["manifest"].get("dataset_sha256")
        self.feature_names = tuple(self.simulator.features[0]["names"])
        self.action_space = spaces.Discrete(len(Action))
        self.observation_space = spaces.Dict({
            "market": spaces.Box(-1, 1, shape=(len(self.feature_names),), dtype=np.float32),
            "market_valid": spaces.MultiBinary(len(self.feature_names)),
            "portfolio": spaces.Box(-1, 1, shape=(len(PORTFOLIO_NAMES),), dtype=np.float32),
            "action_mask": spaces.MultiBinary(len(Action)),
        })
        self._reset_called = False

    def action_masks(self):
        s = self.simulator
        # These describe portfolio constraints, not a prediction that the next
        # quote will satisfy execution conditions.
        return np.array([True, not s.quantity and not s.pending_sell,
                         bool(s.quantity and not s.partial_taken and not s.pending_sell),
                         bool(s.quantity)], dtype=bool)

    def _observation(self):
        s = self.simulator
        raw = s.observation()
        initial = s.config.initial_cash
        equity = max(s.equity, 0)
        risk = s.quantity * max(s.entry - s.hard_stop, 0)
        daily_remaining = max(equity * s.risk.daily_loss_limit_pct - max(-s.realized, 0), 0)
        aggregate_remaining = max(equity * s.risk.max_aggregate_open_risk_pct - risk, 0)
        now = datetime.fromtimestamp(raw["decision_ts"], ZoneInfo("America/New_York"))
        angle = (now.hour * 3600 + now.minute * 60 + now.second) / 86400 * 2 * math.pi
        weekday = now.weekday() / 7 * 2 * math.pi
        ratios = (
            s.cash / initial, (s.equity - initial) / initial,
            s.quantity * s.last_bid / initial, s.realized / initial,
            (s.equity - s.cash - s.quantity * s.cost_basis) / initial,
            (s.peak_equity - initial) / initial, s.max_drawdown / initial,
            risk / initial, daily_remaining / initial, aggregate_remaining / initial,
        )
        position = bool(s.quantity)
        portfolio = [*map(_bounded, ratios), float(position), float(s.partial_taken),
                     s.pending_sell / s.quantity if position else 0,
                     _bounded(raw["portfolio"]["holding_seconds"] / 3600),
                     _bounded((s.last_bid - s.stop) / s.entry) if position else 0,
                     _bounded(s.last_bid / s.entry - 1) if position else 0,
                     _bounded(s.peak / s.entry - 1) if position else 0,
                     float(s.last_bid > 0), float(position and s._quote(s.index) is None),
                     math.sin(angle), math.cos(angle), math.sin(weekday), math.cos(weekday)]
        return {"market": np.array(raw["market"], dtype=np.float32),
                "market_valid": np.array(raw["valid"], dtype=np.int8),
                "portfolio": np.array(portfolio, dtype=np.float32),
                "action_mask": self.action_masks().astype(np.int8)}

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        if options:
            raise ValueError("Episode selection options are not supported yet")
        self.simulator.reset()
        self._reset_called = True
        return self._observation(), {"environment_version": VERSION,
                                     "source_dataset_sha256": self.source_digest,
                                     "broker_calls": 0}

    def step(self, action):
        if not self._reset_called:
            raise RuntimeError("Call reset before step")
        if isinstance(action, (bool, np.bool_)) or not self.action_space.contains(action):
            raise ValueError("Action must be a valid discrete action index")
        _, reward, terminated, truncated, event = self.simulator.step(int(action))
        return self._observation(), float(reward), bool(terminated), bool(truncated), event
