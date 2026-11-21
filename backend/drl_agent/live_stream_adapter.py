"""Bounded adapter for existing normalized Schwab Redis snapshots; no broker IO."""
from collections import deque
from copy import deepcopy
import math

from drl_agent.dataset import _normalize
from drl_agent.live_execution import ExecutionQuote
from drl_agent.live_features import encode_live


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('invalid_numeric_field')
    return value


def _shares(value):
    value = _number(value)
    if value < 0 or int(value) != value:
        raise ValueError('invalid_share_count')
    return int(value)


def _clock(payload, field, now, age):
    stamp = _number(payload.get(field))
    if stamp <= 0 or not 0 <= now * 1000 - stamp <= age * 1000:
        raise ValueError('stale_or_future_' + field)
    return stamp


def _record(symbol, kind, payload, event):
    received = payload['received_at_ms']
    record = dict(symbol=symbol, source='schwab_stream', observation_type=kind,
        observation_key=f"{symbol}:{kind}:{event}:{received}",
        event_ts=event / 1000, received_ts=received / 1000, payload=deepcopy(payload))
    if kind == 'minute_candle':
        record.update({k: payload[k] for k in ('open', 'high', 'low', 'close', 'volume')})
    return dict(record=record)


class LiveStreamAdapter:
    def __init__(self, symbols):
        symbols = tuple(symbols)
        if (not 1 <= len(symbols) <= 50 or len(set(symbols)) != len(symbols)
                or not all(isinstance(s, str) and s and s == s.strip().upper() for s in symbols)):
            raise ValueError('invalid_symbol_roster')
        self.history = {symbol: {'levelone_quote': deque(maxlen=2), 'level2_book': deque(maxlen=2)}
                        for symbol in symbols}
        self.last_timestamp = {}

    def adapt(self, snapshot, *, timestamp, account, scanner_record=None):
        symbol = snapshot.get('symbol')
        if symbol not in self.history:
            raise ValueError('symbol_not_in_roster')
        _number(timestamp)
        if timestamp <= self.last_timestamp.get(symbol, 0):
            raise ValueError('nonincreasing_snapshot')
        q, book = snapshot.get('levelone') or {}, snapshot.get('book') or {}
        if not q:
            raise ValueError('quote_unavailable')
        if not book:
            raise ValueError('book_unavailable')
        if q.get('symbol') != symbol or book.get('symbol') != symbol:
            raise ValueError('payload_symbol_mismatch')
        if q.get('source') != 'schwab_stream' or book.get('source') != 'schwab_stream':
            raise ValueError('unsupported_source')
        if q.get('delayed') is not False or str(q.get('security_status', '')).lower() != 'normal':
            raise ValueError('delayed_or_abnormal_quote')
        venue = 'NASDAQ_BOOK' if q.get('exchange_id') == 'Q' else 'NYSE_BOOK' if q.get('exchange_id') in {'N', 'A', 'P'} else None
        if venue is None or book.get('service') != venue:
            raise ValueError('listing_book_mismatch')
        clocks = [_clock(q, key, timestamp, 3) for key in ('quote_time_ms', 'bid_time_ms', 'ask_time_ms')]
        received = _clock(q, 'received_at_ms', timestamp, 3)
        event = max(clocks)
        if q.get('trade_time_ms') is not None:
            trade = _number(q['trade_time_ms'])
            if trade > timestamp * 1000:
                raise ValueError('future_trade_time')
            event = max(event, trade)
        book_event = _clock(book, 'market_time_ms', timestamp, 2)
        book_received = _clock(book, 'received_at_ms', timestamp, 2)
        bid, ask = _number(q.get('bid')), _number(q.get('ask'))
        if not 0 < bid <= ask:
            raise ValueError('invalid_nbbo')
        sizes = []
        for side, price in (('bids', bid), ('asks', ask)):
            levels = book.get(side)
            if not isinstance(levels, list) or not 1 <= len(levels) <= 100:
                raise ValueError('invalid_book_depth')
            prices = [_number(level.get('price')) for level in levels]
            shares = [_shares(level.get('size')) for level in levels]
            if min(prices) <= 0 or prices != sorted(set(prices), reverse=side == 'bids'):
                raise ValueError('invalid_book_order')
            # Venue depth away from the NBBO is not executable at the NBBO price.
            if not math.isclose(prices[0], price, rel_tol=0, abs_tol=1e-8):
                raise ValueError('book_nbbo_mismatch')
            sizes.append(shares[0])
        raw_candles = snapshot.get('candles') or []
        if not isinstance(raw_candles, list) or len(raw_candles) > 1000:
            raise ValueError('candle_window_exceeded')
        candles = {}
        for candle in raw_candles:
            try:
                if candle.get('symbol') != symbol or candle.get('source') != 'schwab_stream':
                    continue
                stamp, receipt = _number(candle.get('chart_time_ms')), _number(candle.get('received_at_ms'))
                if stamp <= 0 or receipt <= 0 or max(stamp + 60000, receipt) > timestamp * 1000:
                    continue
                o, h, l, c, v = [_number(candle.get(k)) for k in ('open', 'high', 'low', 'close', 'volume')]
                if not 0 < l <= min(o, c) <= max(o, c) <= h or v < 0:
                    continue
                if stamp not in candles or receipt > candles[stamp]['received_at_ms']:
                    candles[stamp] = candle
            except (ValueError, TypeError, AttributeError):
                continue
        selected = [candles[stamp] for stamp in sorted(candles)[-120:]]
        if not selected or timestamp * 1000 - selected[-1]['chart_time_ms'] > 120000:
            raise ValueError('completed_volume_unavailable')
        volume = sum(c['volume'] for c in selected[-20:]) / len(selected[-20:])
        execution = ExecutionQuote(symbol, timestamp, min(clocks) / 1000, received / 1000,
            book_event / 1000, book_received / 1000, bid, ask,
            _shares(q.get('bid_size_shares')), _shares(q.get('ask_size_shares')), *sizes, volume)
        inputs = {kind: list(rows) for kind, rows in self.history[symbol].items()}
        for kind, payload, stamp in (('levelone_quote', q, event), ('level2_book', book, book_event)):
            row = _record(symbol, kind, payload, stamp)
            rows = inputs[kind]
            if not rows or row['record']['observation_key'] != rows[-1]['record']['observation_key']:
                rows.append(row)
            inputs[kind] = rows[-2:]
        inputs['minute_candle'] = [_record(symbol, 'minute_candle', c, c['chart_time_ms']) for c in selected]
        inputs['scanner'] = []
        if scanner_record is not None:
            row = _normalize(scanner_record, scanner=True)
            if (scanner_record.get('symbol') != symbol or scanner_record.get('source') != 'scanner_shadow'
                    or row['available_ms'] > timestamp * 1000):
                raise ValueError('invalid_scanner_provenance')
            inputs['scanner'] = [dict(record=deepcopy(scanner_record))]
        state = dict(symbol=symbol, decision_ts=timestamp, inputs=inputs)
        features = encode_live(state, account)
        for kind in self.history[symbol]:
            self.history[symbol][kind] = deque(deepcopy(inputs[kind]), maxlen=2)
        self.last_timestamp[symbol] = timestamp
        return dict(state=state, features=features, execution_quote=execution,
                    candidate_admission_verified=False, broker_calls=0)
