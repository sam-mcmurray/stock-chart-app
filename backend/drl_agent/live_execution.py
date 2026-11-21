"""Single-account shadow execution for streaming fixtures; never submits orders."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import math

import exchange_calendars as xcals

from drl_agent.live_recurrent import TARGET_EXPOSURES
from drl_agent.simulator import SimulationConfig
from services.trade_risk import RiskLimits, calculate_long_position_size, evaluate_long_exit


@dataclass(frozen=True)
class ExecutionQuote:
    symbol: str
    timestamp: float
    event_ts: float
    received_ts: float
    book_event_ts: float
    book_received_ts: float
    bid: float
    ask: float
    bid_shares: int
    ask_shares: int
    book_bid_shares: int
    book_ask_shares: int
    average_minute_volume: float
    delayed: bool = False
    normal: bool = True


class LiveShadowExecution:
    """One symbol, one simulated account; no shared-portfolio performance claims.

    Normalized book sizes must describe the quote's top prices, not total depth.
    A stream adapter still needs to verify matching book venue and provenance.
    Same-session sale proceeds are not recycled into buying power.
    """
    def __init__(self, symbol, session_date, *, config=None):
        self.config = config or replace(SimulationConfig(), initial_cash=500)
        c = self.config
        if (not symbol or not all(math.isfinite(v) for v in vars(c).values())
                or c.initial_cash <= 0 or not 0 < c.displayed_participation <= 1
                or not 0 <= c.fee_bps < 10000 or not 0 <= c.slippage_bps < 10000
                or not 0 < c.stop_fraction < 1 or not 0 < c.minimum_price <= c.maximum_price
                or not 0 < c.latency_seconds <= 3 or c.max_spread_bps <= 0):
            raise ValueError('Invalid shadow configuration')
        day = datetime.strptime(session_date, '%Y-%m-%d').date()
        cal = xcals.get_calendar('XNYS', start=day - timedelta(days=7), end=day + timedelta(days=7))
        if not cal.is_session(session_date):
            raise ValueError('Not a trading session')
        self.open, self.close = (cal.session_open(session_date).timestamp(), cal.session_close(session_date).timestamp())
        self.symbol = symbol
        self.session_date = session_date
        self.session_report = None
        self.daily_realized_baseline = 0.
        self.carry_boundary = None
        self.risk = RiskLimits(slippage_bps=c.slippage_bps)
        self.cash = self.equity = c.initial_cash
        self.quantity = 0
        self.entry = self.cost_basis = self.stop = self.active_stop = self.peak = 0.
        self.realized = self.fees = self.purchased = 0.
        self.pending = None
        self.latest = None
        self.last_fill_event = -math.inf
        self.partial_taken = False

    @property
    def buying_power(self):
        return max(0., min(self.cash, self.config.initial_cash - self.purchased))

    def _fresh(self, q):
        clocks = (q.timestamp, q.event_ts, q.received_ts, q.book_event_ts, q.book_received_ts)
        return (all(math.isfinite(t) for t in clocks)
            and self.open <= q.timestamp < self.close
            and all(0 <= q.timestamp - t <= age for t, age in
                ((q.event_ts, 3), (q.received_ts, 3), (q.book_event_ts, 2), (q.book_received_ts, 2)))
            and q.delayed is False and q.normal is True)

    def _size(self, q, price, stop, *, incremental=False):
        c = self.config
        limits = self.risk
        reserved = self.quantity * (max(0, self.entry - self.stop)
            + self.entry * c.slippage_bps / 10000 + (self.entry + self.stop) * c.fee_bps / 10000)
        if incremental and self.quantity and self.equity > 0:
            limits = replace(limits,
                risk_per_trade_pct=max(0, limits.risk_per_trade_pct - reserved / self.equity),
                max_position_pct=max(0, limits.max_position_pct - self.quantity * price / self.equity))
        sizing = calculate_long_position_size(account_equity=self.equity,
            buying_power=self.buying_power if incremental else self.buying_power + self.quantity * price,
            entry_price=price, stop_price=stop, daily_realized_pnl=self.realized - self.daily_realized_baseline,
            aggregate_open_risk=reserved if incremental else 0,
            average_minute_volume=q.average_minute_volume, limits=limits)
        cost_risk = sizing.risk_per_share + (price + stop) * c.fee_bps / 10000
        return min(sizing.quantity, math.floor(sizing.risk_budget / cost_risk)) if cost_risk > 0 else 0

    def propose(self, action):
        if type(action) is not int or not 0 <= action < len(TARGET_EXPOSURES):
            raise ValueError('Invalid target exposure')
        q = self.latest
        if self.session_report is not None or q is None or not self._fresh(q):
            return False
        if self.pending and self.pending['protective']:
            return False
        if q.timestamp >= self.close - 300 and action != 0:
            return False
        c = self.config
        price = q.ask * (1 + c.slippage_bps / 10000)
        stop = self.stop if self.quantity else price * (1 - c.stop_fraction)
        target = math.floor(self._size(q, price, stop) * TARGET_EXPOSURES[action])
        self.pending = dict(target=target, timestamp=q.timestamp, stop=stop,
            protective=target < self.quantity, reason='policy_reduction')
        return True

    def advance(self, q):
        if self.session_report is not None:
            return {'reason': 'session_settled', 'fill': None, 'equity': None}
        if (q.symbol != self.symbol or not math.isfinite(q.timestamp)
                or not all(math.isfinite(v) for v in (q.bid, q.ask, q.average_minute_volume))
                or not 0 < q.bid <= q.ask or q.average_minute_volume < 0
                or any(type(v) is not int or v < 0 for v in
                    (q.bid_shares, q.ask_shares, q.book_bid_shares, q.book_ask_shares))):
            raise ValueError('Invalid execution quote')
        if self.latest is not None and q.timestamp <= self.latest.timestamp:
            return {'reason': 'duplicate_or_old_frame', 'fill': None, 'equity': None}
        self.latest = q
        if not self._fresh(q):
            if self.pending and not self.pending['protective']:
                self.pending = None
            return {'reason': 'market_unavailable', 'fill': None, 'equity': None}
        c = self.config
        mark = q.bid * (1 - c.slippage_bps / 10000) * (1 - c.fee_bps / 10000)
        self.equity = self.cash + self.quantity * mark
        overnight = None
        if self.carry_boundary is not None:
            overnight = {**self.carry_boundary, 'observed_ts': q.timestamp,
                'marked_equity': self.equity,
                'net_return': (self.equity - self.carry_boundary['reference_equity']) / c.initial_cash,
                'includes_unobserved_gap': True, 'training_attributed': False}
            self.carry_boundary = None
        fill, reason = None, 'hold'
        costs = dict(spread=0., slippage=0., fees=0.)
        if q.timestamp >= self.close - 300 and self.pending and self.pending['target'] > self.quantity:
            self.pending = None
        order = self.pending
        if order:
            elapsed = q.timestamp - order['timestamp']
            fresh_fill = min(q.event_ts, q.received_ts, q.book_event_ts, q.book_received_ts) >= order['timestamp'] + c.latency_seconds
            if not order['protective'] and elapsed > 3:
                self.pending, reason = None, 'expired_intent'
            elif elapsed >= c.latency_seconds and fresh_fill and q.event_ts > self.last_fill_event:
                delta = order['target'] - self.quantity
                buying = delta > 0
                price = (q.ask * (1 + c.slippage_bps / 10000) if buying
                         else q.bid * (1 - c.slippage_bps / 10000))
                displayed = min(q.ask_shares, q.book_ask_shares) if buying else min(q.bid_shares, q.book_bid_shares)
                quantity = min(abs(delta), math.floor(displayed * c.displayed_participation))
                if buying:
                    spread = (q.ask - q.bid) / ((q.ask + q.bid) / 2) * 10000
                    if (not c.minimum_price <= price <= c.maximum_price or spread > c.max_spread_bps
                            or (self.quantity and price <= max(self.entry, self.active_stop))):
                        quantity = 0
                    quantity = min(quantity, self._size(q, price, order['stop'], incremental=True),
                        math.floor(self.buying_power / (price * (1 + c.fee_bps / 10000))))
                if quantity > 0:
                    fee = quantity * price * c.fee_bps / 10000
                    if buying:
                        cost = quantity * price + fee
                        total = self.quantity + quantity
                        self.entry = (self.entry * self.quantity + quantity * price) / total
                        self.cost_basis = (self.cost_basis * self.quantity + cost) / total
                        self.cash -= cost
                        self.purchased += cost
                        self.quantity = total
                        self.stop = order['stop']
                        self.active_stop = max(self.active_stop, self.stop)
                    else:
                        self.cash += quantity * price - fee
                        self.realized += quantity * (price - self.cost_basis) - fee
                        self.quantity -= quantity
                        if order['protective'] and order.get('reason') == 'partial_profit_target':
                            self.partial_taken = True
                    self.fees += fee
                    self.last_fill_event = q.event_ts
                    fill = dict(side='buy' if buying else 'sell', quantity=quantity, price=price, fee=fee)
                    costs = dict(spread=quantity * (q.ask - q.bid) / 2,
                        slippage=quantity * abs(price - (q.ask if buying else q.bid)), fees=fee)
                    reason = 'filled' if quantity == abs(delta) else 'partial_fill'
                else:
                    reason = 'risk_or_liquidity_blocked'
                if not order['protective'] or self.quantity <= order['target']:
                    self.pending = None
            else:
                reason = 'waiting_for_later_quote'
        if not self.quantity:
            self.entry = self.cost_basis = self.stop = self.active_stop = self.peak = 0.
            self.partial_taken = False
        else:
            self.peak = max(self.peak, q.bid)
            decision = evaluate_long_exit(quantity=self.quantity, entry_price=self.entry,
                hard_stop_price=self.stop, active_stop_price=self.active_stop, current_price=q.bid,
                peak_price=self.peak, partial_taken=self.partial_taken)
            self.active_stop = max(self.active_stop, decision.active_stop)
            if decision.action in {'exit_all', 'take_partial'}:
                target = max(0, self.quantity - decision.quantity)
                if not self.pending or target < self.pending['target']:
                    self.pending = dict(target=target, timestamp=q.timestamp, stop=self.stop,
                        protective=True, reason=decision.reason)
            if q.timestamp >= self.close - 300 and (not self.pending or self.pending['target'] != 0):
                self.pending = dict(target=0, timestamp=q.timestamp, stop=self.stop,
                    protective=True, reason='end_of_day_flat')
        mark = q.bid * (1 - c.slippage_bps / 10000) * (1 - c.fee_bps / 10000)
        self.equity = self.cash + self.quantity * mark
        return dict(reason=reason, fill=fill, equity=self.equity, cash=self.cash,
            quantity=self.quantity, fees=self.fees, buying_power=self.buying_power,
            pending_exit=bool(self.pending and self.pending['protective']), costs=costs,
            inventory_fraction=self.quantity * q.bid / c.initial_cash, overnight_outcome=overnight, broker_calls=0)

    def settle_session(self, timestamp):
        """Idempotent close report, not liquidation or a fresh market valuation."""
        if not math.isfinite(timestamp) or timestamp < self.close:
            raise ValueError('Session has not closed')
        if self.session_report is None:
            self.pending = (dict(target=0, timestamp=self.close, stop=self.stop,
                protective=True, reason='unresolved_session_close') if self.quantity else None)
            self.session_report = dict(session_date=self.session_date,
                status='unresolved_position' if self.quantity else 'flat', quantity=self.quantity,
                cash=self.cash, last_marked_equity=self.equity, realized_pnl=self.realized,
                valuation_stale=bool(self.quantity), fill=None, broker_calls=0)
        return dict(self.session_report)

    def start_session(self, session_date):
        """Carry the ledger forward, never replace it with a fresh bankroll.

        Buying power stays conservative until a separate settlement reconciliation.
        Overnight outcomes are evidence, not an invented on-policy transition.
        """
        if self.session_report is None:
            raise ValueError('Settle the previous session first')
        candidate = LiveShadowExecution(self.symbol, session_date, config=self.config)
        if candidate.open <= self.close:
            raise ValueError('New session must follow the previous session')
        if self.quantity and self.carry_boundary is None:
            self.carry_boundary = dict(from_session=self.session_date, reference_equity=self.equity,
                reference_close=self.close, quantity=self.quantity,
                inventory_fraction=max(0., self.equity - self.cash) / self.config.initial_cash)
        self.session_date = session_date
        self.open, self.close = candidate.open, candidate.close
        self.daily_realized_baseline = self.realized
        self.session_report = None
        self.latest = None
        self.pending = (dict(target=0, timestamp=self.open, stop=self.stop,
            protective=True, reason='overnight_flatten') if self.quantity else None)
