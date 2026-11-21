"""Offline learned portfolio decisions; no broker access or live model promotion."""
from dataclasses import dataclass, field
from datetime import datetime
import math
from zoneinfo import ZoneInfo

import numpy as np
import torch

from drl_agent.agent import PPOAgent
from drl_agent.simulator import Action

VERSION = "portfolio-ppo-v1"
ACCOUNT_FIELDS = ("cash", "equity", "realized_pnl", "fees", "aggregate_open_risk",
                  "settled_buying_power", "unsettled_sale_proceeds", "max_drawdown")


@dataclass
class Rollout:
    states: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    logprobs: list = field(default_factory=list)
    action_masks: list = field(default_factory=list)
    rewards: list = field(default_factory=list)
    is_terminals: list = field(default_factory=list)


class PortfolioEncoder:
    """Ranked current candidates plus holdings; never encode ticker identity/future bars.

    One discretionary symbol action per replay tick. Protective exits continue
    independently for every holding inside PortfolioReplay.
    """

    def __init__(self, feature_names, slots=40):
        if not 1 <= slots <= 200:
            raise ValueError("slots must be 1..200")
        self.feature_names = tuple(feature_names)
        self.slots = slots
        self.slot_width = 2 * len(self.feature_names) + 7
        self.state_dim = len(ACCOUNT_FIELDS) + 4 + slots * self.slot_width
        self.action_dim = 1 + slots * 3

    def encode(self, observation):
        account = observation["account"]
        positions = account["positions"]
        watching = [row["symbol"] for row in observation["watching"]]
        eligible = {row["symbol"] for row in observation["watching"] if not row["entry_block_reasons"]}
        symbols = list(dict.fromkeys([*sorted(positions), *watching]))
        if len(symbols) > self.slots:
            raise ValueError("Visible universe exceeds policy slots; refusing to drop holdings/candidates")
        def bound(value):
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("Nonfinite policy input")
            return value / (1 + abs(value))
        values = [bound(account[key] / 500) for key in ACCOUNT_FIELDS]
        now = datetime.fromtimestamp(observation["decision_ts"], ZoneInfo("America/New_York"))
        angle = (now.hour * 3600 + now.minute * 60 + now.second) * math.tau / 86400
        weekday = now.weekday() * math.tau / 7
        values.extend((math.sin(angle), math.cos(angle), math.sin(weekday), math.cos(weekday)))
        mask = np.zeros(self.action_dim, dtype=bool)
        mask[0] = True
        for index, symbol in enumerate(symbols):
            row = observation["symbols"][symbol]
            if len(row["market"]) != len(self.feature_names) or len(row["valid"]) != len(self.feature_names):
                raise ValueError("Incompatible market features")
            position = positions.get(symbol, {})
            quantity = position.get("quantity", 0)
            pending = position.get("pending_sell", 0)
            values.extend([*row["market"], *row["valid"], 1, float(symbol in watching),
                           bound(quantity / 1000), bound(position.get("entry", 0) / 50),
                           bound(position.get("active_stop", 0) / 50),
                           bound(pending / 1000), float(bool(quantity))])
            mask[1 + index * 3:4 + index * 3] = (
                symbol in eligible and not pending and account["settled_buying_power"] > 0,
                quantity > 1 and not pending, quantity > 0)
        values.extend([0] * ((self.slots - len(symbols)) * self.slot_width))
        state = np.asarray(values, dtype=np.float32)
        if not np.isfinite(state).all():
            raise ValueError("Nonfinite policy input")
        return state, mask, symbols

    @staticmethod
    def decode(action, symbols):
        if action == 0:
            return {}
        index, kind = divmod(int(action) - 1, 3)
        return {symbols[index]: (Action.ENTER, Action.TAKE_PARTIAL, Action.EXIT)[kind]}


def make_agent(encoder):
    return PPOAgent(encoder.state_dim, encoder.action_dim, lr=0.0003,
                    betas=(0.9, 0.999), gamma=0.99, K_epochs=4, eps_clip=0.2)


class LearnedPolicy:
    def __init__(self, agent, encoder):
        self.agent, self.encoder = agent, encoder

    def __call__(self, observation):
        state, mask, symbols = self.encoder.encode(observation)
        with torch.no_grad():
            distribution = self.agent.policy_old.distribution(
                torch.from_numpy(state).reshape(1, -1), torch.from_numpy(mask).reshape(1, -1))
            action = int(distribution.probs.argmax(-1).item())
        return self.encoder.decode(action, symbols)


def learn_session(replay, agent, encoder):
    memory = Rollout()
    observation = replay.observation()
    while not replay.done:
        state, mask, symbols = encoder.encode(observation)
        action = agent.select_action(state, memory, action_mask=mask)
        observation, reward, terminated, truncated, _ = replay.step(encoder.decode(action, symbols))
        memory.rewards.append(reward)
        # Finite-session objective: retain the marked position at the boundary;
        # never manufacture a fill or continue into an unrecorded session.
        memory.is_terminals.append(terminated or truncated)
    agent.update(memory)
    return {"transitions": len(memory.rewards), "reward": sum(memory.rewards),
            "account": replay.account(),
            "unresolved_position": bool(replay.account()["positions"]),
            "fills": sum(row["fill"] is not None for event in replay.events
                         for row in event["symbols"].values())}
