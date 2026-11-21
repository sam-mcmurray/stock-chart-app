"""Single-session synthetic execution mechanics, not a strategy or live adapter."""
from copy import deepcopy
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import math
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from drl_agent.historical_news_join import _verify
from drl_agent.dataset import encode_dataset
from drl_agent.simulator import EntryBudget, SimulationConfig
from services.trade_risk import ExitPolicy, RiskLimits, calculate_long_position_size, evaluate_long_exit


class SyntheticExecution:
    """IOC test orders, persistent protective exits and next-state fills.

    This tests mechanics only. It does not certify historical Scanner eligibility,
    supply a trained policy, simulate queue priority, or invent intrabar fills.
    """

    def __init__(self, joined, *, session_date, config=None):
        _verify(joined, "historical-news-price-join-v1", "join_sha256")
        depth = joined.get("synthetic_depth")
        if not depth:
            raise ValueError("Explicit synthetic depth required")
        _verify(depth, "synthetic-depth-v1", "scenario_sha256")
        if depth["symbol"] != joined["symbol"] or not depth.get("synthetic"):
            raise ValueError("Invalid synthetic provenance")
        if depth["input_sha256"] != hashlib.sha256(encode_dataset(joined["candles"]).encode()).hexdigest():
            raise ValueError("Synthetic depth input mismatch")
        self.config = config or replace(SimulationConfig(), initial_cash=500)
        c = self.config
        if (any(isinstance(v, bool) or not math.isfinite(float(v)) for v in asdict(c).values())
                or c.initial_cash <= 0 or not 0 < c.displayed_participation <= 1
                or not 0 <= c.fee_bps < 10000 or not 0 <= c.slippage_bps < 10000
                or c.latency_seconds <= 0 or not 0 < c.minimum_price <= c.maximum_price
                or c.max_spread_bps <= 0):
            raise ValueError("Invalid execution configuration")
        day = datetime.strptime(session_date, "%Y-%m-%d").date()
        cal = xcals.get_calendar("XNYS", start=day - timedelta(days=7), end=day + timedelta(days=7))
        if not cal.is_session(session_date):
            raise ValueError("Not a trading session")
        self.open = int(cal.session_open(session_date).timestamp())
        self.close = int(cal.session_close(session_date).timestamp())
        self.states = deepcopy([s for s in joined["states"]
                                if datetime.fromtimestamp(s["decision_ts"], ZoneInfo("America/New_York")).date() == day])
        if len(self.states) < 2 or any(a["decision_ts"] >= b["decision_ts"]
                                     for a, b in zip(self.states, self.states[1:])):
            raise ValueError("At least two increasing session states required")
        self.books = {}
        for book in depth["snapshots"]:
            identity = book["source_candle_id"]
            if identity in self.books or not book.get("synthetic") or book["symbol"] != joined["symbol"]:
                raise ValueError("Invalid or duplicate book")
            for side in ("bids", "asks"):
                levels = book[side]
                if not levels or any(type(x["size"]) is not int or x["size"] < 0
                                     or isinstance(x["price"], bool) or not math.isfinite(x["price"])
                                     or x["price"] <= 0 for x in levels):
                    raise ValueError("Invalid book levels")
                prices = [x["price"] for x in levels]
                if prices != sorted(set(prices), reverse=side == "bids"):
                    raise ValueError("Book prices must be strictly ordered")
            if book["bids"][0]["price"] >= book["asks"][0]["price"]:
                raise ValueError("Crossed synthetic book")
            self.books[identity] = deepcopy(book)
        self.candles = {r["id"]: deepcopy(r) for r in joined["candles"]}
        for state in self.states:
            book = self.books.get(state["candle_id"])
            candle = self.candles.get(state["candle_id"])
            if (not book or not candle or state["decision_ts"] != candle["end_ts"]
                    or book["available_ts"] != state["decision_ts"]
                    or book["source_bar_end_ts"] != candle["end_ts"]
                    or book["source_bar_start_ts"] != candle["start_ts"]):
                raise ValueError("State/candle/book timing mismatch")
        self.symbol, self.source_hash = joined["symbol"], joined["join_sha256"]
        self.index = 0
        self.cash = c.initial_cash
        self.quantity = 0
        self.cost_basis = self.purchase_cost = self.realized = self.fees = 0.
        self.events = []
        self.risk = RiskLimits(slippage_bps=c.slippage_bps)
        self.exit_policy = ExitPolicy()
        self.entry = self.hard_stop = self.active_stop = self.peak = 0.
        self.partial_taken = False
        self.pending_exit = 0
        self.pending_exit_reason = None
        self.completed_round_trips = 0

    def _protective_exit(self):
        if not self.quantity:
            return
        book = self.books[self.states[self.index]["candle_id"]]
        bids = [x for x in book["bids"] if x["size"]]
        if not bids:
            self.pending_exit, self.pending_exit_reason = self.quantity, "market_data_unavailable"
            return
        bid = bids[0]["price"]
        self.peak = max(self.peak, bid)
        decision = evaluate_long_exit(quantity=self.quantity, entry_price=self.entry,
            hard_stop_price=self.hard_stop, current_price=bid, peak_price=self.peak,
            partial_taken=self.partial_taken, active_stop_price=self.active_stop, policy=self.exit_policy)
        self.active_stop = max(self.active_stop, decision.active_stop)
        if decision.action == "exit_all":
            self.pending_exit, self.pending_exit_reason = self.quantity, decision.reason
        elif decision.action == "take_partial" and not self.pending_exit:
            self.pending_exit, self.pending_exit_reason = decision.quantity, decision.reason

    def _buy_size(self, price, stop, bid, volume, fee_per_share, requested, budget=None):
        c = self.config
        if self.quantity and (price < self.entry or price <= self.active_stop):
            return 0, "scale_in_below_entry_or_active_stop"
        power = max(0, min(self.cash, c.initial_cash - self.purchase_cost))
        equity = self.cash + self.quantity * bid * (1 - c.slippage_bps / 10000) * (1 - c.fee_bps / 10000)
        reserved = self.quantity * (max(0, self.entry - self.hard_stop)
                    + self.entry * c.slippage_bps / 10000
                    + (self.entry + self.hard_stop) * c.fee_bps / 10000)
        budget = budget or EntryBudget(equity, power, self.realized, reserved)
        equity, power = budget.account_equity, min(power, budget.buying_power)
        if equity <= 0:
            return 0, "invalid_account_equity"
        limits = self.risk
        if self.quantity:
            limits = replace(limits,
                risk_per_trade_pct=max(0, limits.risk_per_trade_pct - reserved / equity),
                max_position_pct=max(0, limits.max_position_pct - self.quantity * price / equity))
        sizing = calculate_long_position_size(account_equity=equity, buying_power=power,
            entry_price=price, stop_price=stop, daily_realized_pnl=budget.daily_realized_pnl,
            aggregate_open_risk=budget.aggregate_open_risk, average_minute_volume=volume, limits=limits)
        risk_cost = sizing.risk_per_share + fee_per_share + stop * c.fee_bps / 10000
        qty = min(requested, sizing.quantity, math.floor(power / (price + fee_per_share)),
                  math.floor(sizing.risk_budget / risk_cost) if risk_cost > 0 else 0)
        return qty, sizing.reason or "liquidity_or_cost_cap"

    @property
    def done(self):
        return self.index == len(self.states) - 1

    @staticmethod
    def validate_order(*, side="hold", quantity=0, limit_price=None, stop_price=None):
        if side not in {"hold", "buy", "sell"} or type(quantity) is not int or quantity < 0:
            raise ValueError("Invalid order")
        if side == "hold" and quantity:
            raise ValueError("Hold must have zero quantity")
        if side != "hold":
            if quantity < 1 or isinstance(limit_price, bool) or not isinstance(limit_price, (float, int)) or not math.isfinite(limit_price) or limit_price <= 0:
                raise ValueError("Orders require positive integer shares and a finite limit")
            if side == "buy" and (isinstance(stop_price, bool) or not isinstance(stop_price, (float, int))
                                  or not math.isfinite(stop_price) or not 0 < stop_price < limit_price):
                raise ValueError("Buy requires a valid risk-sizing stop")

    def step(self, *, side="hold", quantity=0, limit_price=None, stop_price=None,
             entry_budget=None, entry_block_reason=None):
        if self.done:
            raise RuntimeError("Session finished")
        self.validate_order(side=side, quantity=quantity, limit_price=limit_price, stop_price=stop_price)
        if entry_budget is not None and not isinstance(entry_budget, EntryBudget):
            raise ValueError("Invalid shared entry budget")
        proposed_side, proposed_quantity = side, quantity
        self._protective_exit()
        if self.pending_exit:
            if side == "sell" and quantity > self.pending_exit:
                self.pending_exit = min(quantity, self.quantity)
            side, quantity, limit_price = "sell", self.pending_exit, 0.
        exit_reason = self.pending_exit_reason if self.pending_exit else None
        automatic_exit = bool(self.pending_exit)
        proposed = self.states[self.index]["decision_ts"]
        self.index += 1
        state = self.states[self.index]
        now = state["decision_ts"]
        book = self.books[state["candle_id"]]
        bids = [x for x in book["bids"] if x["size"]]
        asks = [x for x in book["asks"] if x["size"]]
        reason, fill = "hold", None
        if side != "hold":
            if side == "buy" and entry_block_reason:
                reason = entry_block_reason
            elif not self.open <= proposed < now < self.close:
                reason = "outside_regular_session"
            elif now - proposed < self.config.latency_seconds or now - proposed > 60:
                reason = "missing_next_minute_or_latency"
            elif not bids or (side == "buy" and not asks):
                reason = "book_empty"
            else:
                level = asks[0] if side == "buy" else bids[0]
                sign = 1 if side == "buy" else -1
                price = level["price"] * (1 + sign * self.config.slippage_bps / 10000)
                spread = ((asks[0]["price"] - bids[0]["price"]) / ((asks[0]["price"] + bids[0]["price"]) / 2) * 10000
                          if side == "buy" else 0.)
                cap = math.floor(level["size"] * self.config.displayed_participation)
                fee_per_share = price * self.config.fee_bps / 10000
                qty = min(quantity, cap)
                if (side == "buy" and price > limit_price) or (side == "sell" and price < limit_price):
                    reason = "limit_not_marketable"
                elif side == "buy" and spread > self.config.max_spread_bps:
                    reason = "spread_too_wide"
                elif side == "buy" and not self.config.minimum_price <= price <= self.config.maximum_price:
                    reason = "price_out_of_range"
                else:
                    if side == "buy":
                        stop_price = self.hard_stop if self.quantity else stop_price
                        qty, reason = self._buy_size(price, stop_price, bids[0]["price"],
                            self.candles[state["candle_id"]]["volume"], fee_per_share, qty, entry_budget)
                    else:
                        qty = min(qty, self.quantity)
                        reason = "no_position_or_liquidity"
                    if qty > 0:
                        fee = qty * fee_per_share
                        if side == "buy":
                            cost = qty * price + fee
                            self.cash -= cost
                            self.purchase_cost += cost
                            old = self.quantity
                            self.quantity += qty
                            self.entry = (old * self.entry + qty * price) / self.quantity
                            self.cost_basis = (old * self.cost_basis + cost) / self.quantity
                            if not old:
                                self.hard_stop = self.active_stop = stop_price
                                self.partial_taken = False
                            self.peak = max(self.peak, price)
                        else:
                            self.cash += qty * price - fee
                            self.realized += qty * (price - self.cost_basis) - fee
                            self.quantity -= qty
                            if automatic_exit:
                                self.pending_exit -= qty
                                if exit_reason == "partial_profit_target":
                                    self.partial_taken = True
                                if not self.pending_exit:
                                    self.pending_exit_reason = None
                            if not self.quantity:
                                self.cost_basis = self.entry = self.hard_stop = self.active_stop = self.peak = 0.
                                self.pending_exit = 0
                                self.pending_exit_reason = None
                                self.completed_round_trips += 1
                        self.fees += fee
                        fill = {"side": side, "quantity": qty, "price": price, "fee": fee,
                                "synthetic": True, "source_candle_id": state["candle_id"]}
                        reason = "filled" if qty == quantity else "partial_fill"
        event = {"decision_ts": proposed, "execution_ts": now, "side": side,
                 "proposed_side": proposed_side, "proposed_quantity": proposed_quantity,
                 "exit_signal": exit_reason, "pending_exit": self.pending_exit,
                 "entry_price": self.entry, "hard_stop": self.hard_stop, "active_stop": self.active_stop,
                 "completed_round_trips": self.completed_round_trips,
                 "requested_quantity": quantity, "fill": fill, "reason": reason,
                 "unfilled_quantity": quantity - (fill["quantity"] if fill else 0),
                 "time_in_force": "persistent_exit_intent" if automatic_exit else "IOC",
                 "cash": self.cash, "quantity": self.quantity,
                 "settled_buying_power": max(0, min(self.cash, self.config.initial_cash - self.purchase_cost)),
                 "realized_pnl": self.realized, "fees": self.fees,
                 "open_at_end": self.done and self.quantity > 0}
        self.events.append(event)
        return deepcopy(event)

    def report(self):
        return {"version": "synthetic-execution-v2", "source_join_sha256": self.source_hash,
                "symbol": self.symbol, "synthetic": True, "initial_cash": self.config.initial_cash,
                "config": asdict(self.config), "risk_limits": asdict(self.risk), "exit_policy": asdict(self.exit_policy),
                "cash": self.cash, "quantity": self.quantity, "realized_pnl": self.realized,
                "fees": self.fees, "events": deepcopy(self.events), "finished": self.done,
                "open_at_end": self.done and self.quantity > 0,
                "pending_exit": self.pending_exit, "completed_round_trips": self.completed_round_trips,
                "training_ready": False, "live_activation_eligible": False, "broker_calls": 0,
                "limitations": ["Execution mechanics only; no Scanner eligibility or strategy admission",
                    "Single-session engine; use synthetic_competition for separate daily/weekly comparisons",
                    "Automatic exits are triggered at recorded decision states, not intrabar or broker stops",
                    "Top nonempty level only; manual IOC remainders cancel, protective intents persist",
                    "Minute-close synthetic fills do not reproduce intrabar paths or exchange queues"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--joined", type=Path, required=True)
    parser.add_argument("--session", required=True, help="New York session date")
    parser.add_argument("--orders", type=Path, help="JSON array of explicit test orders with decision_index")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    sim = SyntheticExecution(json.loads(args.joined.read_text()), session_date=args.session)
    orders = json.loads(args.orders.read_text()) if args.orders else []
    if not isinstance(orders, list):
        raise ValueError("Orders must be an array")
    planned = {}
    for raw in orders:
        order = dict(raw)
        index = order.pop("decision_index")
        if type(index) is not int or not 0 <= index < len(sim.states) - 1 or index in planned:
            raise ValueError("Invalid or duplicate order decision index")
        planned[index] = order
    while not sim.done:
        sim.step(**planned.get(sim.index, {}))
    result = sim.report()
    result["orders_sha256"] = hashlib.sha256(encode_dataset(orders).encode()).hexdigest()
    result["report_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({**{k: v for k, v in result.items() if k != "events"},
                          "fill_count": sum(e["fill"] is not None for e in result["events"])}))


if __name__ == "__main__":
    main()
