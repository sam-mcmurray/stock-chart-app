from datetime import datetime, timezone
import json

import pytest

from drl_agent.live_inputs import RedisLiveInputs, scanner_admission
from drl_agent.watch_replay import WatchConfig

NOW = 1795190400


class FakeRedis:
    def __init__(self):
        self.data = {}; self.commands = []; self.executions = 0; self.fail = False

    def pipeline(self, *, transaction):
        assert transaction is False
        parent = self
        class Pipe:
            def __init__(self): self.ops = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def hget(self, key, field): self.ops.append(('hget', key, field)); return self
            def hmget(self, key, fields): self.ops.append(('hmget', key, fields)); return self
            def execute(self):
                parent.executions += 1
                parent.commands = self.ops
                if parent.fail: raise RuntimeError('offline')
                return [parent.data.get((k, f)) if op == 'hget' else [parent.data.get((k, x)) for x in f]
                        for op, k, f in self.ops]
        return Pipe()


def test_batched_bounded_reads_and_candle_cache():
    redis = FakeRedis()
    redis.data[('schwab:stream:levelone', 'ABC')] = json.dumps({'symbol': 'ABC', 'exchange_id': 'Q', 'last': 5})
    candle_key = ('schwab:stream:chart:ABC', str((NOW // 60 - 1) * 60000))
    redis.data[candle_key] = json.dumps({'close': 5})
    reader = RedisLiveInputs(redis, ['ABC', 'DEF'])
    result = reader.read(NOW)
    assert redis.executions == 1 and len(redis.commands) == 8
    assert all(len(fields) == 120 for op, key, fields in redis.commands if op == 'hmget')
    result['ABC']['candles'][0]['close'] = 999
    redis.data[candle_key] = json.dumps({'close': 6})
    redis.data[('schwab:stream:levelone', 'ABC')] = json.dumps({'symbol': 'ABC', 'last': 6})
    second = reader.read(NOW + 1)
    assert len(redis.commands) == 6 and redis.executions == 2
    assert second['ABC']['levelone']['last'] == 6 and second['ABC']['candles'][0]['close'] == 5
    assert reader.read(NOW + 15)['ABC']['candles'][0]['close'] == 6
    assert len(redis.commands) == 8
    reader.read(NOW + 60)
    assert len(redis.commands) == 8


def test_failed_read_does_not_publish_cached_market_as_fresh():
    redis = FakeRedis(); reader = RedisLiveInputs(redis, ['ABC'])
    reader.read(NOW)
    before = dict(reader.cache)
    redis.fail = True
    with pytest.raises(RuntimeError): reader.read(NOW + 20)
    assert reader.cache == before


def record(rvol=4, shares=1_000_000):
    return dict(id='fixture', symbol='ABC', source='scanner_shadow', ts=NOW,
        created_at=datetime.fromtimestamp(NOW, timezone.utc).isoformat(), payload=dict(
            watch_inputs=dict(version='scanner-watch-inputs-v1', observed_at=NOW,
                              price=5, rvol=rvol, float_shares=shares),
            semantic_signal={'hype': {'evidence_article_ids': ['article-1'], 'as_of_ts': NOW}},
            market_signal={'technicals': {'macd_open': False}}))


def admission(row, **changes):
    params = dict(symbol='ABC', timestamp=NOW, current_price=5,
        config=WatchConfig(1, 20, 3, 20_000_000, 50))
    params.update(changes)
    return scanner_admission(row, **params)


def test_watch_before_momentum_without_rule_based_macd_gate():
    watching = admission(record(rvol=1))
    assert watching['watch_allowed'] and not watching['entry_inputs_ready']
    assert watching['reasons'] == ['rvol_below_threshold']
    triggered = admission(record())
    assert triggered['watch_allowed'] and triggered['entry_inputs_ready']
    missing = admission(record(shares=None))
    assert missing['watch_allowed'] and missing['reasons'] == ['float_unknown']


def test_missing_news_stale_scanner_and_current_price_rechecked():
    row = record(); row['payload']['semantic_signal']['hype']['evidence_article_ids'] = []
    assert admission(row)['reasons'] == ['news_evidence_missing']
    assert admission(record(), timestamp=NOW + 901)['reasons'] == ['scanner_stale']
    assert admission(record(), current_price=30)['reasons'] == ['current_price_out_of_range']
    assert admission(record(), timestamp=NOW - 1)['reasons'] == ['scanner_not_yet_available']
    assert not admission(record(), symbol='XYZ')['watch_allowed']
    assert not admission(record(), current_price=float('nan'))['watch_allowed']


def test_reader_rejects_excessive_roster():
    with pytest.raises(ValueError): RedisLiveInputs(FakeRedis(), [f'S{i}' for i in range(51)])
