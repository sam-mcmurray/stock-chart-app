"""Deterministic single-symbol shadow transitions over recorded decision states."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum
import math
from zoneinfo import ZoneInfo

from drl_agent.dataset import VERSION as DATASET_VERSION, _normalize
from drl_agent.features import encode_state
from services.market_stream_store import book_metrics
from services.decision_engine import market_session
from services.position_exit_controller import candle_momentum_reversal
from services.trade_risk import RiskLimits, ExitPolicy, calculate_long_position_size, evaluate_long_exit

VERSION = "drl-simulator-v1"


class Action(IntEnum):
    HOLD = 0
    ENTER = 1
    TAKE_PARTIAL = 2
    EXIT = 3


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash: float = 10_000
    fee_bps: float = 1
    slippage_bps: float = 10
    minimum_price: float = 1
    maximum_price: float = 50
    minimum_hype: float = 0.25
    stop_fraction: float = 0.02
    max_spread_bps: float = 100
    displayed_participation: float = 0.25
    latency_seconds: float = 1
    drawdown_penalty: float = 0.1


@dataclass(frozen=True)
class EntryBudget:
    account_equity: float
    buying_power: float
    daily_realized_pnl: float
    aggregate_open_risk: float

    def __post_init__(self):
        if (not all(math.isfinite(float(v)) for v in asdict(self).values())
                or self.buying_power < 0 or self.aggregate_open_risk < 0):
            raise ValueError("Invalid shared entry budget")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


class TradingSimulator:
    """Actions use the next recorded decision state; missing fills stay missing.

    This coarse replay cannot reconstruct intra-step stops or exchange queue
    priority. No end-of-data liquidation is fabricated.
    """

    def __init__(self, dataset: dict, *, config: SimulationConfig | None = None):
        if dataset.get("manifest", {}).get("version") != DATASET_VERSION:
            raise ValueError("Unsupported observation dataset")
        self.config = config or SimulationConfig()
        c = self.config
        if (not all(math.isfinite(float(v)) for v in asdict(c).values())
                or c.initial_cash <= 0 or not 0 < c.minimum_price <= c.maximum_price
                or not 0 < c.stop_fraction < 1 or not 0 < c.displayed_participation <= 1
                or not 0 <= c.minimum_hype <= 1 or not 0 <= c.fee_bps < 10000
                or not 0 <= c.slippage_bps < 10000 or c.latency_seconds <= 0
                or c.max_spread_bps <= 0 or c.drawdown_penalty < 0):
            raise ValueError("Invalid simulator configuration")
        self.states = deepcopy(dataset["states"])
        if len(self.states) < 2:
            raise ValueError("At least two states required")
        times = [int(s["decision_ts"]) for s in self.states]
        days = {datetime.fromtimestamp(t, ZoneInfo("America/New_York")).date() for t in times}
        symbols = {s["symbol"] for s in self.states}
        if any(b <= a for a, b in zip(times, times[1:])) or len(days) != 1 or len(symbols) != 1:
            raise ValueError("States must increase within one symbol and New York session date")
        self.features = [encode_state(s) for s in self.states]
        self.risk = RiskLimits(slippage_bps=c.slippage_bps)
        self.policy = ExitPolicy()
        self.reset()

    def reset(self):
        self.index = 0
        self.cash = self.equity = self.peak_equity = self.config.initial_cash
        self.quantity = self.pending_sell = 0
        self.entry = self.cost_basis = self.stop = self.hard_stop = self.peak = 0.0
        self.realized = self.fees = self.max_drawdown = self.last_bid = 0.0
        self.partial_taken = False
        self.entry_ts = None
        self.used_quotes = set()
        self.events = []
        self.done = False
        return self.observation()

    def _rows(self, index, kind):
        allowed = set(self.features[index]["source_ids"][kind])
        rows = [_normalize(item["record"], scanner=kind == "scanner")
                for item in self.states[index]["inputs"].get(kind, []) if item["id"] in allowed]
        return sorted(rows, key=lambda r: (r["event_ms"], r["received_ms"], r["id"]))

    def _quote(self, index):
        rows = self._rows(index, "levelone_quote")
        if not rows:
            return None
        row = rows[-1]
        q = row["record"]["payload"]
        now = self.states[index]["decision_ts"] * 1000
        bid, ask = number(q.get("bid")), number(q.get("ask"))
        clocks = [number(q.get(k)) for k in ("quote_time_ms", "bid_time_ms", "ask_time_ms")]
        if (not 0 < bid <= ask or q.get("delayed") is not False
                or str(q.get("security_status", "")).lower() != "normal"
                or now - row["received_ms"] > 3000
                or any(t <= 0 or not 0 <= now - t <= 3000 for t in clocks)):
            return None
        return row

    def observation(self):
        feature = self.features[self.index]
        now = self.states[self.index]["decision_ts"]
        return {"market": list(feature["values"]), "valid": list(feature["valid"]),
                "portfolio": {"cash": self.cash, "equity": self.equity,
                              "quantity": self.quantity, "entry_price": self.entry,
                              "active_stop": self.stop, "realized_pnl": self.realized,
                              "partial_taken": self.partial_taken, "pending_sell": self.pending_sell,
                              "holding_seconds": now - self.entry_ts if self.entry_ts is not None else 0},
                "decision_ts": now}

    def step(self, action: int, *, entry_budget: EntryBudget | None = None, allow_scale_in=False):
        if self.done:
            raise RuntimeError("Episode ended; reset before stepping")
        action = Action(action)
        c = self.config
        proposed_at = self.states[self.index]["decision_ts"]
        scanner_rows = self._rows(self.index, "scanner")
        scanner_row = scanner_rows[-1] if scanner_rows else None
        scanner = scanner_row["record"]["payload"] if scanner_row else {}
        prior_equity, prior_drawdown = self.equity, self.max_drawdown
        self.index += 1
        now = self.states[self.index]["decision_ts"]
        quote = self._quote(self.index)
        reason, fill = "hold", None
        if quote:
            self.last_bid = number(quote["record"]["payload"]["bid"])
        if self.quantity and quote:
            self.peak = max(self.peak, self.last_bid)
            books = self._rows(self.index, "level2_book")
            metrics = book_metrics(books[-1]["record"]["payload"], now_ms=now * 1000) if books else {}
            flow_fresh = (metrics.get("service") in {"NASDAQ_BOOK", "NYSE_BOOK"}
                          and metrics.get("market_age_seconds") is not None
                          and metrics.get("receive_age_seconds") is not None
                          and max(metrics["market_age_seconds"], metrics["receive_age_seconds"]) <= 2)
            candles = [r["record"] for r in self._rows(self.index, "minute_candle")]
            exit_check = evaluate_long_exit(
                quantity=self.quantity, entry_price=self.entry, hard_stop_price=self.hard_stop,
                current_price=self.last_bid, peak_price=self.peak, active_stop_price=self.stop,
                partial_taken=self.partial_taken, policy=self.policy,
                momentum_reversal=candle_momentum_reversal(candles, current_price=self.last_bid),
                order_flow_imbalance=metrics.get("imbalance") if flow_fresh else None)
            self.stop = max(self.stop, exit_check.active_stop)
            if exit_check.action == "exit_all":
                self.pending_sell = self.quantity
                reason = exit_check.reason
        if self.quantity and action == Action.EXIT:
            self.pending_sell = self.quantity
            reason = reason if reason != "hold" else "policy_exit"
        elif self.quantity and action == Action.TAKE_PARTIAL and not self.partial_taken and not self.pending_sell:
            self.pending_sell = max(1, self.quantity // 2)
            self.partial_taken = True
            reason = "policy_partial"
        new_quote = bool(quote and quote["id"] not in self.used_quotes
                         and min(quote["event_ms"], quote["received_ms"]) >= (proposed_at + c.latency_seconds) * 1000)
        if self.pending_sell:
            if new_quote:
                q = quote["record"]["payload"]
                qty = min(self.quantity, self.pending_sell,
                          math.floor(max(0, number(q.get("bid_size_shares"))) * c.displayed_participation))
                if qty:
                    price = self.last_bid * (1 - c.slippage_bps / 10000)
                    fee = qty * price * c.fee_bps / 10000
                    self.cash += qty * price - fee
                    self.realized += qty * (price - self.cost_basis) - fee
                    self.quantity -= qty
                    self.pending_sell -= qty
                    self.fees += fee
                    fill = {"side": "sell", "quantity": qty, "price": price, "fee": fee}
                    self.used_quotes.add(quote["id"])
            if fill is None:
                reason = "exit_waiting_for_liquidity" if new_quote else "exit_waiting_for_fresh_quote"
        elif action == Action.ENTER and (not self.quantity or allow_scale_in):
            reason, fill = self._enter(scanner_row, scanner, quote if new_quote else None, proposed_at, now,
                                       entry_budget=entry_budget)
        elif action == Action.ENTER:
            reason = "already_in_position"
        elif not quote:
            reason = "quote_unavailable"
        if not self.quantity:
            self.entry = self.cost_basis = self.stop = self.hard_stop = self.peak = 0.0
            self.entry_ts = None
        mark = self.last_bid * (1 - c.slippage_bps / 10000) * (1 - c.fee_bps / 10000)
        self.equity = self.cash + self.quantity * mark
        self.peak_equity = max(self.peak_equity, self.equity)
        self.max_drawdown = max(self.max_drawdown, self.peak_equity - self.equity)
        reward = ((self.equity - prior_equity) - c.drawdown_penalty * (self.max_drawdown - prior_drawdown)) / c.initial_cash
        self.done = self.index == len(self.states) - 1
        event = {"decision_ts": proposed_at, "evaluation_ts": now, "proposed_action": action.name,
                 "reason": reason, "fill": fill, "cash": self.cash, "equity": self.equity,
                 "quantity": self.quantity, "pending_sell": self.pending_sell,
                 "valuation_stale": quote is None and self.quantity > 0,
                 "truncated": self.done, "open_at_end": self.done and self.quantity > 0,
                 "quote_id": quote["id"] if quote else None, "reward": reward,
                 "broker_calls": 0}
        self.events.append(event)
        return self.observation(), reward, False, self.done, deepcopy(event)

    def _enter(self, scanner_row, scanner, quote, proposed_at, now, *, entry_budget=None):
        c = self.config
        if not scanner_row or proposed_at * 1000 - scanner_row["event_ms"] > 900_000:
            return "scanner_unavailable", None
        hype = ((scanner.get("semantic_signal") or {}).get("hype") or {})
        if number(hype.get("long_hype_score")) < c.minimum_hype:
            return "hype_below_threshold", None
        if not quote:
            return "entry_waiting_for_fresh_quote", None
        q = quote["record"]["payload"]
        if (q.get("regular_market_quote") is not True
                or not market_session(datetime.fromtimestamp(now, timezone.utc))["is_regular"]):
            return "outside_regular_session", None
        bid, ask = number(q["bid"]), number(q["ask"])
        if not c.minimum_price <= ask <= c.maximum_price:
            return "price_out_of_range", None
        if (ask - bid) / ((ask + bid) / 2) * 10000 > c.max_spread_bps:
            return "spread_too_wide", None
        books = self._rows(self.index, "level2_book")
        book = books[-1] if books else None
        service = "NASDAQ_BOOK" if q.get("exchange_id") == "Q" else "NYSE_BOOK" if q.get("exchange_id") in {"N", "A", "P"} else None
        if (not book or not service or book["record"]["payload"].get("service") != service
                or max(now * 1000 - book["event_ms"], now * 1000 - book["received_ms"]) > 2000):
            return "book_unavailable", None
        features = self.features[self.index]
        if dict(zip(features["names"], features["values"]))["book.valid"] != 1:
            return "book_invalid", None
        depth = book_metrics(book["record"]["payload"], now_ms=now * 1000)
        if depth["bid_depth"] <= 0 or depth["ask_depth"] <= 0:
            return "book_empty", None
        candles = self._rows(self.index, "minute_candle")
        if not candles or now * 1000 - candles[-1]["event_ms"] > 120_000:
            return "volume_unavailable", None
        volume = sum(max(0, number(r["record"].get("volume"))) for r in candles) / len(candles)
        price = ask * (1 + c.slippage_bps / 10000)
        if price > c.maximum_price:
            return "price_out_of_range", None
        stop = number((scanner.get("trade_plan") or {}).get("stop_loss")) or price * (1 - c.stop_fraction)
        if self.quantity:
            if price <= self.stop:
                return "scale_in_below_active_stop", None
            stop = self.hard_stop
        fee_per_share = price * c.fee_bps / 10000
        budget = entry_budget or EntryBudget(self.equity, max(self.cash, 0), self.realized, 0)
        limits = self.risk
        if self.quantity and budget.account_equity > 0:
            reserved = self.quantity * (max(self.entry - self.hard_stop, 0)
                        + self.entry * c.slippage_bps / 10000
                        + (self.entry + self.hard_stop) * c.fee_bps / 10000)
            limits = replace(limits,
                risk_per_trade_pct=max(0, limits.risk_per_trade_pct - reserved / budget.account_equity),
                max_position_pct=max(0, limits.max_position_pct - self.quantity * price / budget.account_equity))
        sizing = calculate_long_position_size(account_equity=budget.account_equity, buying_power=budget.buying_power,
            entry_price=price, stop_price=stop, daily_realized_pnl=budget.daily_realized_pnl,
            aggregate_open_risk=budget.aggregate_open_risk,
            average_minute_volume=volume, limits=limits)
        if not sizing.allowed:
            return sizing.reason, None
        risk_with_costs = sizing.risk_per_share + fee_per_share + stop * c.fee_bps / 10000
        qty = min(sizing.quantity, math.floor(budget.buying_power / (price + fee_per_share)),
                  math.floor(sizing.risk_budget / risk_with_costs),
                  math.floor(depth["ask_depth"] * c.displayed_participation),
                  math.floor(max(0, number(q.get("ask_size_shares"))) * c.displayed_participation))
        if qty < 1:
            return "liquidity_or_cost_cap", None
        fee = qty * fee_per_share
        self.cash -= qty * price + fee
        old_quantity = self.quantity
        total = old_quantity + qty
        self.entry = (old_quantity * self.entry + qty * price) / total
        self.cost_basis = (old_quantity * self.cost_basis + qty * (price + fee_per_share)) / total
        self.quantity = total
        self.hard_stop = stop
        self.stop = max(self.stop, stop)
        self.peak = max(self.peak, price)
        if not old_quantity:
            self.entry_ts, self.partial_taken = now, False
        self.fees += fee
        self.used_quotes.add(quote["id"])
        return "entry", {"side": "buy", "quantity": qty, "price": price, "fee": fee}
