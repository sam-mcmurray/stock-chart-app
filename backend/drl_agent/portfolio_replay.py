"""Single-session, shared-account replay of aligned multi-symbol observations."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib

from drl_agent.dataset import encode_dataset
from drl_agent.simulator import Action, EntryBudget, SimulationConfig, TradingSimulator
from drl_agent.watch_replay import replay_watch_windows

VERSION = "drl-portfolio-replay-v3"


class PortfolioReplay:
    def __init__(self, datasets, *, watch_config, config=None, prior_round_trip_timestamps=()):
        if not datasets or sum(len(d.get("states", [])) for d in datasets) > 100000:
            raise ValueError("Provide at most 100000 aggregate states")
        self.config = config or SimulationConfig(initial_cash=500)
        # The watch price bounds remain hard execution bounds, even if a quote
        # moves after admission. Never widen the simulator's existing limits.
        self.config = replace(self.config,
                              minimum_price=max(self.config.minimum_price, watch_config.minimum_price),
                              maximum_price=min(self.config.maximum_price, watch_config.maximum_price))
        self.watch_config = watch_config
        self.simulators = {}
        self.source_hashes = []
        rows = {}
        times = None
        for dataset in datasets:
            sim = TradingSimulator(dataset, config=self.config)
            symbol = sim.states[0]["symbol"]
            grid = [state["decision_ts"] for state in sim.states]
            if symbol in self.simulators or (times is not None and grid != times):
                raise ValueError("Require distinct symbols on identical decision grids")
            times = grid
            self.simulators[symbol] = sim
            self.source_hashes.append(hashlib.sha256(encode_dataset(dataset).encode()).hexdigest())
            for state in sim.states:
                for item in state["inputs"].get("scanner", []):
                    raw = item["record"]
                    if raw.get("symbol") == symbol:
                        rows[encode_dataset(raw)] = raw
        self.times = times
        self.prior_round_trip_timestamps = tuple(prior_round_trip_timestamps)
        if any(isinstance(t, bool) or not isinstance(t, int) or not 0 < t <= times[0]
               for t in self.prior_round_trip_timestamps):
            raise ValueError("Prior round trips must have valid timestamps no later than replay start")
        step = times[1] - times[0]
        if any(b - a != step for a, b in zip(times, times[1:])):
            raise ValueError("Require a uniform decision grid")
        self.watch = replay_watch_windows(list(rows.values()), start_ts=times[0], end_ts=times[-1],
                                         step_seconds=step, config=watch_config)
        self.reset()

    def reset(self):
        for sim in self.simulators.values():
            sim.reset()
        self.index = 0
        self.peak_equity = self.config.initial_cash
        self.max_drawdown = 0.0
        self.events = []
        self.done = False
        self.round_trip_timestamps = list(self.prior_round_trip_timestamps)
        self.purchase_cost = 0.0
        self.unsettled_proceeds = 0.0
        return self.observation()

    def account(self):
        sims = self.simulators.values()
        c = self.config
        cash = c.initial_cash + sum(s.cash - c.initial_cash for s in sims)
        equity = cash + sum(s.quantity * s.last_bid * (1 - c.slippage_bps / 10000)
                            * (1 - c.fee_bps / 10000) for s in sims)
        # Keep original hard-stop risk reserved; do not treat unrealized gains
        # or a ratcheted stop as permission to add unbounded new exposure.
        risk = sum(s.quantity * (max(s.entry - s.hard_stop, 0)
                   + s.entry * c.slippage_bps / 10000
                   + (s.entry + s.hard_stop) * c.fee_bps / 10000) for s in sims)
        return {"cash": cash, "equity": equity, "realized_pnl": sum(s.realized for s in sims),
                "fees": sum(s.fees for s in sims), "aggregate_open_risk": risk,
                "settled_buying_power": max(0, min(cash, c.initial_cash - self.purchase_cost)),
                "unsettled_sale_proceeds": self.unsettled_proceeds,
                "completed_round_trips": len(self.round_trip_timestamps),
                "max_drawdown": self.max_drawdown,
                "positions": {symbol: {"quantity": s.quantity, "entry": s.entry,
                                        "active_stop": s.stop, "pending_sell": s.pending_sell}
                              for symbol, s in sorted(self.simulators.items()) if s.quantity}}

    def observation(self):
        watching = self.watch["frames"][self.index]["watching"]
        visible = {c["symbol"] for c in watching} | set(self.account()["positions"])
        return {"decision_ts": self.times[self.index], "account": self.account(),
                "watching": deepcopy(watching),
                "symbols": {symbol: {k: v for k, v in self.simulators[symbol].observation().items()
                                      if k != "portfolio"} for symbol in sorted(visible)}}

    def step(self, actions):
        if self.done:
            raise RuntimeError("Episode ended; reset before stepping")
        if not isinstance(actions, dict) or any(s not in self.simulators for s in actions):
            raise ValueError("Actions must map known symbols to discrete actions")
        if any(isinstance(a, bool) or not isinstance(a, (int, Action)) for a in actions.values()):
            raise ValueError("Invalid action")
        proposals = {symbol: Action(actions.get(symbol, Action.HOLD)) for symbol in self.simulators}
        before = self.account()
        current = self.watch["frames"][self.index]["watching"]
        eligible = {c["symbol"] for c in current if not c["entry_block_reasons"]}
        next_eligible = {c["symbol"] for c in self.watch["frames"][self.index + 1]["watching"]
                         if not c["entry_block_reasons"]}
        blocked = set()
        for symbol, action in proposals.items():
            if action == Action.ENTER and (symbol not in eligible or symbol not in next_eligible):
                proposals[symbol] = Action.HOLD
                blocked.add(symbol)
        # Mark every held symbol at the execution timestamp before allocating
        # entry budgets, independent of iteration order. Policies saw only t.
        for sim in self.simulators.values():
            quote = sim._quote(sim.index + 1)
            if quote:
                sim.last_bid = float(quote["record"]["payload"]["bid"])
        entrants = [c["symbol"] for c in current if proposals[c["symbol"]] == Action.ENTER]
        order = sorted(set(self.simulators) - set(entrants)) + entrants
        transitions = {}
        for symbol in order:
            sim = self.simulators[symbol]
            account = self.account()
            stale_positions = any(s.quantity and s._quote(self.index + 1) is None
                                  for s in self.simulators.values())
            action = proposals[symbol]
            stale_entry = action == Action.ENTER and stale_positions
            if stale_entry:
                action = Action.HOLD
            budget = EntryBudget(account["equity"], account["settled_buying_power"],
                                 account["realized_pnl"], account["aggregate_open_risk"])
            old_quantity = sim.quantity
            *_, event = sim.step(action, entry_budget=budget, allow_scale_in=True)
            event["proposed_action"] = Action(actions.get(symbol, Action.HOLD)).name
            if (symbol in blocked or stale_entry) and event["fill"] is None:
                event["reason"] = "stale_portfolio_valuation" if stale_entry else "outside_watch_targets"
            if event["fill"]:
                fill = event["fill"]
                if fill["side"] == "buy":
                    self.purchase_cost += fill["quantity"] * fill["price"] + fill["fee"]
                else:
                    self.unsettled_proceeds += fill["quantity"] * fill["price"] - fill["fee"]
                    if old_quantity and not sim.quantity:
                        self.round_trip_timestamps.append(event["evaluation_ts"])
            # Local simulators retain per-symbol ledgers; expose shared account
            # amounts only in the enclosing portfolio transition.
            transitions[symbol] = {k: v for k, v in event.items() if k not in {"cash", "equity", "reward"}}
        self.index += 1
        account = self.account()
        self.peak_equity = max(self.peak_equity, account["equity"])
        old_drawdown = self.max_drawdown
        self.max_drawdown = max(old_drawdown, self.peak_equity - account["equity"])
        reward = ((account["equity"] - before["equity"])
                  - self.config.drawdown_penalty * (self.max_drawdown - old_drawdown)) / self.config.initial_cash
        self.done = self.index == len(self.times) - 1
        event = {"decision_ts": self.times[self.index - 1], "evaluation_ts": self.times[self.index],
                 "account": self.account(), "symbols": transitions, "reward": reward,
                 "truncated": self.done, "open_at_end": self.done and bool(account["positions"]),
                 "broker_calls": 0}
        self.events.append(event)
        return self.observation(), reward, False, self.done, deepcopy(event)

    def report(self):
        return {"version": VERSION, "source_hashes": self.source_hashes,
                "config": asdict(self.config), "watch_config": asdict(self.watch_config),
                "risk_limits": {symbol: asdict(s.risk) for symbol, s in self.simulators.items()},
                "trade_limit": {"maximum_round_trips": None, "frequency_cap_enabled": False},
                "round_trip_timestamps": list(self.round_trip_timestamps),
                "account": self.account(), "events": deepcopy(self.events),
                "training_ready": False, "broker_calls": 0}
