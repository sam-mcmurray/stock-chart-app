"""Shared-capital synthetic execution on aligned, completed-minute states."""
from copy import deepcopy
from dataclasses import asdict

from drl_agent.simulator import EntryBudget, SimulationConfig
from drl_agent.synthetic_execution import SyntheticExecution
from drl_agent.historical_news_join import _verify


class SyntheticPortfolio:
    def __init__(self, datasets, *, session_date, config=None, watch=None):
        if not datasets or sum(len(d.get("states", [])) for d in datasets) > 100000:
            raise ValueError("Provide at most 100000 aggregate states")
        self.config = config or SimulationConfig(initial_cash=500)
        self.simulators, self.articles = {}, {}
        self.times = None
        for dataset in datasets:
            sim = SyntheticExecution(dataset, session_date=session_date, config=self.config)
            times = [s["decision_ts"] for s in sim.states]
            if sim.symbol in self.simulators or (self.times is not None and times != self.times):
                raise ValueError("Require distinct symbols on identical decision grids")
            self.times = times
            self.simulators[sim.symbol] = sim
            articles = {a["candidate_id"]: a for a in dataset["articles"]}
            for state in sim.states:
                for identity in state["news_candidate_ids"]:
                    if identity not in articles or articles[identity]["replay_available_ts"] > state["decision_ts"]:
                        raise ValueError("Missing or future article in current state")
            self.articles[sim.symbol] = deepcopy(articles)
        self.index = 0
        self.watch = None
        self.watch_hash = None
        self.staged_watch = False
        if watch is not None:
            if watch.get("version") not in {"historical-watch-v1", "historical-watch-v2"}:
                raise ValueError("Unsupported watch version")
            _verify(watch, watch["version"], "watch_sha256")
            self.staged_watch = watch["version"] == "historical-watch-v2"
            if watch["source_hashes"] != {s: sim.source_hash for s, sim in self.simulators.items()}:
                raise ValueError("Watch sources do not match portfolio")
            frames = {f["decision_ts"]: f for f in watch["frames"]}
            if len(frames) != len(watch["frames"]) or any(t not in frames for t in self.times):
                raise ValueError("Watch frames do not match session")
            for frame in frames.values():
                symbols = [c["symbol"] for c in frame["watching"]]
                if len(set(symbols)) != len(symbols) or any(s not in self.simulators for s in symbols):
                    raise ValueError("Invalid watch symbols")
                if self.staged_watch and any(type(c.get("research_entry_ready")) is not bool
                                            for c in frame["watching"]):
                    raise ValueError("Staged watch requires explicit entry readiness")
            self.watch = deepcopy([frames[t] for t in self.times])
            self.watch_hash = watch["watch_sha256"]
        self.purchase_cost = self.unsettled_proceeds = 0.
        self.peak_equity = self.config.initial_cash
        self.max_drawdown = 0.
        self.valuation_incomplete = False
        self.events = []

    @property
    def done(self):
        return self.index == len(self.times) - 1

    def account(self, *, mark_index=None):
        c = self.config
        index = self.index if mark_index is None else mark_index
        cash = c.initial_cash + sum(s.cash - c.initial_cash for s in self.simulators.values())
        value, risk, unpriced, positions = 0., 0., [], {}
        for symbol, sim in sorted(self.simulators.items()):
            if not sim.quantity:
                continue
            book = sim.books[sim.states[index]["candle_id"]]
            bid = next((x["price"] for x in book["bids"] if x["size"]), None)
            if bid is None:
                unpriced.append(symbol)
            else:
                value += sim.quantity * bid * (1 - c.slippage_bps / 10000) * (1 - c.fee_bps / 10000)
            risk += sim.quantity * (max(0, sim.entry - sim.hard_stop) + sim.entry * c.slippage_bps / 10000
                                    + (sim.entry + sim.hard_stop) * c.fee_bps / 10000)
            positions[symbol] = {"quantity": sim.quantity, "entry": sim.entry,
                                 "active_stop": sim.active_stop, "pending_exit": sim.pending_exit}
        return {"cash": cash, "equity": None if unpriced else cash + value,
                "aggregate_open_risk": risk, "unpriced_symbols": unpriced, "positions": positions,
                "realized_pnl": sum(s.realized for s in self.simulators.values()),
                "fees": sum(s.fees for s in self.simulators.values()),
                "completed_round_trips": sum(s.completed_round_trips for s in self.simulators.values()),
                "settled_buying_power": max(0, min(cash, c.initial_cash - self.purchase_cost)),
                "unsettled_sale_proceeds": self.unsettled_proceeds}

    def observation(self):
        symbols = {}
        visible = (set(self.simulators) if self.watch is None else
                   {c["symbol"] for c in self.watch[self.index]["watching"]} | set(self.account()["positions"]))
        for symbol, sim in sorted(self.simulators.items()):
            if symbol not in visible:
                continue
            state = sim.states[self.index]
            symbols[symbol] = {"state": state, "candle": sim.candles[state["candle_id"]],
                               "book": sim.books[state["candle_id"]],
                               "articles": [self.articles[symbol][i] for i in state["news_candidate_ids"]]}
        return deepcopy({"decision_ts": self.times[self.index], "account": self.account(), "symbols": symbols,
                         "watch": self.watch[self.index] if self.watch is not None else None})

    def step(self, orders):
        if self.done:
            raise RuntimeError("Session finished")
        if not isinstance(orders, dict) or any(s not in self.simulators for s in orders):
            raise ValueError("Orders must map known symbols to explicit intents")
        for order in orders.values():
            if not isinstance(order, dict):
                raise ValueError("Invalid order")
            SyntheticExecution.validate_order(**order)
        before = self.account()
        ranks, allowed = {}, set(self.simulators)
        if self.watch is not None:
            ranks = {c["symbol"]: i for i, c in enumerate(self.watch[self.index]["watching"])}
            allowed = set(ranks) & {c["symbol"] for c in self.watch[self.index + 1]["watching"]}
            if self.staged_watch:
                for index in (self.index, self.index + 1):
                    allowed &= {c["symbol"] for c in self.watch[index]["watching"] if c["research_entry_ready"]}
        # Determine protective intent at t, before any next-state budget allocation.
        for sim in self.simulators.values():
            sim._protective_exit()
        order = sorted(self.simulators, key=lambda s: (
            not (self.simulators[s].pending_exit or orders.get(s, {}).get("side") == "sell"),
            ranks.get(s, len(self.simulators)), s))
        transitions = {}
        for symbol in order:
            sim = self.simulators[symbol]
            # Mark all positions at the same execution time, not mixed t/t+1 prices.
            account = self.account(mark_index=self.index + 1)
            unpriced = before["equity"] is None or account["equity"] is None
            budget = EntryBudget(account["equity"] or 0, account["settled_buying_power"],
                                 account["realized_pnl"], account["aggregate_open_risk"])
            event = sim.step(**orders.get(symbol, {}), entry_budget=budget,
                             entry_block_reason=("unpriced_portfolio" if unpriced else
                                                 ("historical_entry_not_ready" if self.staged_watch and symbol in ranks
                                                  else "outside_historical_watch") if symbol not in allowed else None))
            fill = event["fill"]
            if fill:
                if fill["side"] == "buy":
                    self.purchase_cost += fill["quantity"] * fill["price"] + fill["fee"]
                else:
                    self.unsettled_proceeds += fill["quantity"] * fill["price"] - fill["fee"]
            transitions[symbol] = {k: v for k, v in event.items()
                                   if k not in {"cash", "settled_buying_power", "realized_pnl", "fees"}}
        self.index += 1
        account = self.account()
        if account["equity"] is None:
            self.valuation_incomplete = True
        else:
            self.peak_equity = max(self.peak_equity, account["equity"])
            self.max_drawdown = max(self.max_drawdown, self.peak_equity - account["equity"])
        event = {"decision_ts": self.times[self.index - 1], "execution_ts": self.times[self.index],
                 "account": account, "symbols": transitions, "max_drawdown": self.max_drawdown,
                 "open_at_end": self.done and bool(account["positions"]), "broker_calls": 0}
        self.events.append(event)
        return deepcopy(event)

    def report(self):
        return {"version": "synthetic-portfolio-v1", "synthetic": True, "config": asdict(self.config),
                "source_hashes": {s: sim.source_hash for s, sim in sorted(self.simulators.items())},
                "risk_limits": {s: asdict(sim.risk) for s, sim in sorted(self.simulators.items())},
                "account": self.account(), "events": deepcopy(self.events), "finished": self.done,
                "max_drawdown": self.max_drawdown, "valuation_incomplete": self.valuation_incomplete,
                "watch_sha256": self.watch_hash, "watch_gate_enabled": self.watch is not None,
                "allocation_order": "Exits first, then current watch rank if supplied, then symbol",
                "training_ready": False, "live_activation_eligible": False, "broker_calls": 0,
                "limitations": ["Explicit mechanics intents, not certified historical Scanner admission",
                    "Identical decision grids required; no interpolation of missing bars",
                    "Single session; historical settlement regimes are not modeled",
                    "Synthetic minute-close fills are not observed exchange liquidity"]}
