"""Causal live-policy inputs, separate from frozen historical feature schemas."""
import math

import pandas as pd

from drl_agent.dataset import _normalize
from drl_agent.features import FAMILIES, encode_state, _number, _nbbo, _get

VERSION = 'live-level2-features-v1'


def encode_live(state, account):
    cutoff = state['decision_ts'] * 1000
    if any(len(state.get('inputs', {}).get(kind, [])) > 512 for kind in FAMILIES):
        raise ValueError('Live history exceeds bounded feature window')
    base = encode_state(state)
    names, values, valid = list(base['names']), list(base['values']), list(base['valid'])
    histories = {}
    for kind in FAMILIES:
        rows = []
        for item in state.get('inputs', {}).get(kind, []):
            try:
                raw = item['record']
                normalized = _normalize(raw, scanner=kind == 'scanner')
                if (normalized['available_ms'] <= cutoff and normalized['kind'] == kind
                        and str(raw.get('symbol', '')).upper() == base['symbol']
                        and raw.get('source') == ('scanner_shadow' if kind == 'scanner' else 'schwab_stream')):
                    rows.append(normalized)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        # One latest-known revision per event time, not duplicate candles/updates.
        unique = {}
        for row in sorted(rows, key=lambda r: (r['received_ms'], r['id'])):
            unique[row['event_ms']] = row
        histories[kind] = [unique[key] for key in sorted(unique)]

    def add(name, value, scale=1):
        value = _number(value)
        names.append('live.' + name)
        valid.append(int(value is not None))
        values.append(math.tanh(value / scale) if value is not None else 0.0)

    def payload(kind):
        rows = histories[kind]
        return rows[-1]['record'].get('payload', {}) if rows else {}

    quote, scanner = payload('levelone_quote'), payload('scanner')
    nbbo = _nbbo(quote)
    price = nbbo[0] if nbbo else None
    last = _number(quote.get('last'))
    add('mid_price_log', math.log1p(price) if price else None, 5)
    add('last_price_log', math.log1p(last) if last is not None and last > 0 else None, 5)
    vwap = _number(_get(scanner, ('market_signal', 'intraday', 'vwap')))
    add('current_vwap_distance', price / vwap - 1 if price and vwap and vwap > 0 else None, .02)
    rvol = _number(_get(scanner, ('watch_inputs', 'rvol')))
    add('rvol_log', math.log1p(rvol) if rvol is not None and rvol >= 0 else None, 2)
    shares = _number(_get(scanner, ('watch_inputs', 'float_shares')))
    add('float_log', math.log1p(shares) if shares is not None and shares > 0 else None, 20)

    candles = histories['minute_candle']
    for index in range(len(candles) - 1, 0, -1):
        if candles[index]['event_ms'] - candles[index - 1]['event_ms'] != 60000:
            candles = candles[index:]
            break
    closes = [_number(r['record'].get('close')) for r in candles]
    complete = (len(closes) >= 35 and all(c is not None and c > 0 for c in closes)
                and all(b['event_ms'] - a['event_ms'] == 60000 for a, b in zip(candles, candles[1:])))
    add('macd_history_complete', int(complete))
    line = signal = histogram = slope = None
    if complete and price:
        series = pd.Series(closes, dtype=float)
        macd = series.ewm(span=12, adjust=False, min_periods=12).mean() - series.ewm(span=26, adjust=False, min_periods=26).mean()
        signals = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        hist = macd - signals
        line, signal, histogram = float(macd.iloc[-1]), float(signals.iloc[-1]), float(hist.iloc[-1])
        slope = float(hist.iloc[-1] - hist.iloc[-2]) if len(closes) >= 35 else None
    for name, value in (('macd', line), ('macd_signal', signal), ('macd_histogram', histogram), ('macd_histogram_change', slope)):
        add(name, value / price if value is not None and price else None, .01)

    books = histories['level2_book']
    def depth(row, side):
        levels = row['record'].get('payload', {}).get(side) or []
        quantities = [_number(level.get('size')) for level in levels[:5]]
        return sum(quantities) if quantities and all(q is not None and q >= 0 for q in quantities) else None
    same = len(books) >= 2 and books[-1]['service'] == books[-2]['service']
    for side in ('bids', 'asks'):
        previous, current = (depth(books[-2], side), depth(books[-1], side)) if same else (None, None)
        add(f'{side}_depth_change', (current - previous) / max(previous, 1) if previous is not None and current is not None else None)
    add('book_change_interval_seconds', (books[-1]['event_ms'] - books[-2]['event_ms']) / 1000 if same else None, 10)
    stamp = _number(account.get('observed_ts'))
    account_available = stamp is not None and 0 <= state['decision_ts'] - stamp <= 2
    add('account_fresh', int(account_available))
    for field in ('exposure_fraction', 'remaining_risk_fraction', 'unrealized_return', 'drawdown_fraction'):
        add(field, account.get(field) if account_available else None)
    return {**base, 'version': VERSION, 'names': names, 'values': values, 'valid': valid}
