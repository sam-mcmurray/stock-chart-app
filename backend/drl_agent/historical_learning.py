"""Explicit synthetic-depth adapter for historical portfolio PPO research."""
from copy import deepcopy
from dataclasses import replace
import math

from drl_agent.simulator import Action, SimulationConfig
from drl_agent.synthetic_portfolio import SyntheticPortfolio
from drl_agent.historical_news_join import _verify

VERSION = "historical-synthetic-policy-inputs-v1"
FEATURE_NAMES = ("price", "bar_return", "bar_range", "log_volume", "rvol",
                 "vwap_distance", "macd", "macd_signal", "news_count", "news_age",
                 "spread", "bid_depth", "ask_depth", "depth_imbalance",
                 "synthetic_depth", "nominal_price", "venue_volume", "retrospective_news")


class HistoricalLearningReplay:
    def __init__(self, session, *, watch_config=None, config=None, prior_round_trip_timestamps=()):
        watch = session["watch"]
        if watch.get("version") != "historical-watch-v2":
            raise ValueError("Historical learning requires staged watch gates")
        config = config or SimulationConfig(initial_cash=500)
        config = replace(config,
                         minimum_price=max(config.minimum_price, watch["config"]["minimum_price"]),
                         maximum_price=min(config.maximum_price, watch["config"]["maximum_price"]))
        self.engine = SyntheticPortfolio(session["joined"], session_date=session["date"],
                                         config=config, watch=watch)
        self.config = config
        self.times = self.engine.times
        self.events = []
        self.round_trip_timestamps = list(prior_round_trip_timestamps)
        self.source_hashes = [s.source_hash for s in self.engine.simulators.values()]
        self.features = {artifact["symbol"]: {row["decision_ts"]: row for row in artifact["states"]}
                         for artifact in session["features"]}
        for artifact in session["features"]:
            _verify(artifact, "historical-bar-features-v2", "features_sha256")
            if watch["feature_hashes"].get(artifact["symbol"]) != artifact["features_sha256"]:
                raise ValueError("Historical feature identity mismatch")
        if set(self.features) != set(self.engine.simulators):
            raise ValueError("Historical feature symbols mismatch")

    @property
    def done(self):
        return self.engine.done

    def account(self):
        account = self.engine.account()
        if account["equity"] is None:
            raise ValueError("Unpriced historical position; cannot assign a learning reward")
        account["max_drawdown"] = self.engine.max_drawdown
        account["positions"] = {s: {**p, "pending_sell": p["pending_exit"]}
                                for s, p in account["positions"].items()}
        return account

    def observation(self):
        raw = self.engine.observation()
        watching = raw["watch"]["watching"]
        candidates = {row["symbol"]: row for row in watching}
        symbols = {}
        for symbol, data in raw["symbols"].items():
            feature = self.features[symbol][raw["decision_ts"]]
            candle, book = data["candle"], data["book"]
            current = candidates.get(symbol, {})
            close = candle["close"]
            vwap = feature.get("bar_vwap_estimate")
            rvol = current.get("rvol") if current.get("rvol_valid") else None
            bids = [level for level in book["bids"] if level["size"]]
            asks = [level for level in book["asks"] if level["size"]]
            bid_depth, ask_depth = sum(x["size"] for x in bids), sum(x["size"] for x in asks)
            spread = (asks[0]["price"] - bids[0]["price"]) / close if bids and asks else None
            news = [row for row in data["articles"] if row["replay_available_ts"] <= raw["decision_ts"]]
            numbers = (close / 50, close / candle["open"] - 1,
                       (candle["high"] - candle["low"]) / close, math.log1p(candle["volume"]) / 20,
                       rvol / 10 if rvol is not None else None,
                       close / vwap - 1 if vwap and vwap > 0 else None,
                       feature["minute_macd"] / close if feature.get("minute_macd") is not None else None,
                       feature["minute_macd_signal"] / close if feature.get("minute_macd_signal") is not None else None,
                       len(news) / 10,
                       (raw["decision_ts"] - max(row["replay_available_ts"] for row in news)) / 604800 if news else None,
                       spread, bid_depth / 10000, ask_depth / 10000,
                       (bid_depth - ask_depth) / (bid_depth + ask_depth) if bid_depth + ask_depth else None,
                       1, float(feature.get("price_basis") == "nominal"),
                       float(feature["volume_source"] == "iex"), 1)
            valid = [value is not None and math.isfinite(value) for value in numbers]
            symbols[symbol] = {"market": [value / (1 + abs(value)) if ok else 0
                                           for value, ok in zip(numbers, valid)],
                               "valid": [int(ok) for ok in valid]}
        return {"decision_ts": raw["decision_ts"], "account": self.account(),
                "watching": watching, "symbols": symbols}

    def step(self, actions):
        current = self.engine.observation()
        before = self.account()
        orders = {}
        for symbol, action in actions.items():
            if symbol not in current["symbols"] or type(action) not in (int, Action):
                raise ValueError("Action must refer to a visible historical symbol")
            action = Action(action)
            if action == Action.HOLD:
                continue
            data = current["symbols"][symbol]
            position = before["positions"].get(symbol, {})
            side = "buy" if action == Action.ENTER else "sell"
            levels = [x for x in data["book"]["asks" if side == "buy" else "bids"] if x["size"]]
            if not levels:
                continue
            # Limits depend solely on the current completed minute, never t+1.
            tolerance = self.config.slippage_bps / 10000 + 0.002
            limit = levels[0]["price"] * (1 + tolerance if side == "buy" else 1 - tolerance)
            if side == "buy":
                quantity = max(0, math.floor(before["settled_buying_power"] /
                                             (limit * (1 + self.config.fee_bps / 10000))))
            else:
                quantity = position.get("quantity", 0)
                if action == Action.TAKE_PARTIAL:
                    quantity //= 2
            if quantity:
                orders[symbol] = {"side": side, "quantity": quantity, "limit_price": limit}
                if side == "buy":
                    orders[symbol]["stop_price"] = limit * (1 - self.config.stop_fraction)
        event = self.engine.step(orders)
        after = self.account()
        reward = (after["equity"] - before["equity"] - self.config.drawdown_penalty *
                  (after["max_drawdown"] - before["max_drawdown"])) / self.config.initial_cash
        event = {**event, "evaluation_ts": event["execution_ts"], "account": after, "reward": reward}
        for symbol, row in event["symbols"].items():
            if row["fill"] and row["fill"]["side"] == "sell" and not row["quantity"]:
                self.round_trip_timestamps.append(event["execution_ts"])
        self.events.append(event)
        return self.observation(), reward, False, self.done, deepcopy(event)
