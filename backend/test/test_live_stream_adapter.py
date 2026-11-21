from copy import deepcopy

import pytest

from drl_agent.live_stream_adapter import LiveStreamAdapter
from services.market_stream import normalize_levelone_equity, normalize_chart_equity, normalize_equity_book

NOW = 1795190400


def snapshot(now=NOW):
    ms = int(now * 1000)
    q = normalize_levelone_equity(dict(key='ABC', delayed=False,
        **{'1': 5, '2': 5.01, '3': 5., '4': 3, '5': 4, '13': 'Q',
           '32': 'Normal', '34': ms, '35': ms, '37': ms, '38': ms}), received_at_ms=ms)
    book = normalize_equity_book(dict(key='ABC', **{'1': ms,
        '2': [{'0': 5., '1': 123}], '3': [{'0': 5.01, '1': 234}]}),
        service='NASDAQ_BOOK', received_at_ms=ms)
    candles = [normalize_chart_equity(dict(key='ABC', **{'2': 5., '3': 5.1, '4': 4.9,
        '5': 5., '6': 1000., '7': (NOW - n * 60) * 1000}),
        received_at_ms=(NOW - n * 60 + 59) * 1000) for n in range(35, 0, -1)]
    return dict(symbol='ABC', levelone=q, book=book, candles=candles)


def adapt(adapter, data=None, now=NOW):
    return adapter.adapt(data or snapshot(now), timestamp=now, account={'observed_ts': now})


def test_real_normalizers_to_features_without_lot_conversion_twice():
    result = adapt(LiveStreamAdapter(['ABC']))
    quote = result['execution_quote']
    assert (quote.bid_shares, quote.ask_shares) == (300, 400)
    assert (quote.book_bid_shares, quote.book_ask_shares) == (123, 234)
    assert quote.average_minute_volume == 1000
    features = result['features']
    valid = dict(zip(features['names'], features['valid']))
    assert valid['live.macd'] == 1
    assert not result['candidate_admission_verified']


@pytest.mark.parametrize('section,key,value,reason', [
    ('book', 'service', 'NYSE_BOOK', 'listing_book'),
    ('levelone', 'exchange_id', 'unknown', 'listing_book'),
    ('levelone', 'bid_time_ms', (NOW - 10) * 1000, 'stale_or_future'),
    ('book', 'market_time_ms', (NOW + 1) * 1000, 'stale_or_future'),
    ('levelone', 'received_at_ms', (NOW + 1) * 1000, 'stale_or_future'),
    ('levelone', 'trade_time_ms', (NOW + 1) * 1000, 'future_trade'),
    ('levelone', 'delayed', True, 'delayed_or_abnormal'),
    ('book', 'symbol', 'XYZ', 'symbol_mismatch'),
    ('book', 'source', 'synthetic', 'unsupported_source'),
])
def test_rejects_unusable_snapshots_without_committing_history(section, key, value, reason):
    adapter = LiveStreamAdapter(['ABC'])
    data = snapshot(); data[section][key] = value
    with pytest.raises(ValueError, match=reason):
        adapt(adapter, data)
    assert not adapter.history['ABC']['levelone_quote']
    assert not adapter.last_timestamp


def test_book_at_different_price_not_filled_at_nbbo():
    data = snapshot(); data['book']['asks'][0]['price'] = 5.02
    with pytest.raises(ValueError, match='book_nbbo'):
        adapt(LiveStreamAdapter(['ABC']), data)


def test_partial_future_and_other_symbol_candles_are_not_features():
    baseline = adapt(LiveStreamAdapter(['ABC']))
    data = snapshot()
    for stamp, receipt, symbol in ((NOW * 1000, NOW * 1000, 'ABC'),
            ((NOW - 60) * 1000, (NOW + 1) * 1000, 'ABC'),
            ((NOW - 60) * 1000, NOW * 1000, 'XYZ')):
        row = deepcopy(data['candles'][-1])
        row.update(chart_time_ms=stamp, received_at_ms=receipt, symbol=symbol, close=1000)
        data['candles'].append(row)
    result = adapt(LiveStreamAdapter(['ABC']), data)
    assert result['features'] == baseline['features']


def test_history_is_bounded_and_returned_objects_do_not_mutate_it():
    adapter = LiveStreamAdapter(['ABC'])
    for step in range(5):
        result = adapt(adapter, now=NOW + step)
    assert all(len(rows) == 2 for rows in adapter.history['ABC'].values())
    result['state']['inputs']['level2_book'][-1]['record']['payload']['bids'][0]['size'] = 99999
    assert adapter.history['ABC']['level2_book'][-1]['record']['payload']['bids'][0]['size'] == 123
    with pytest.raises(ValueError, match='nonincreasing'):
        adapt(adapter, now=NOW + 4)
    data = snapshot(); data['candles'] = []
    with pytest.raises(ValueError, match='completed_volume'):
        adapt(LiveStreamAdapter(['ABC']), data)
