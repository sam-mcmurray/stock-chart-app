from datetime import datetime, timezone
import json

import pytest

from drl_agent.live_candidates import scanner_rows, load_records, watch_config, select_candidates
from workers.live_learning_readiness_worker import check

NOW = datetime(2026, 11, 20, 16, tzinfo=timezone.utc).timestamp()


def record(symbol, rvol=4):
    return dict(id=symbol, symbol=symbol, source='scanner_shadow', ts=NOW,
        created_at=datetime.fromtimestamp(NOW, timezone.utc), payload=dict(
            watch_inputs=dict(version='scanner-watch-inputs-v1', observed_at=NOW,
                price=5, rvol=rvol, float_shares=1_000_000),
            semantic_signal={'hype': {'evidence_article_ids': ['article']}}))


def row(symbol):
    return dict(symbol=symbol, ts=NOW, last=5, shadow_feature_id=symbol)


def config(capacity=50):
    return watch_config(dict(price_min=1, price_max=20, rvol_threshold=3, float_max=20_000_000), capacity=capacity)


def test_ranks_momentum_retains_low_volume_watch_and_held_first():
    records = {'AAA': record('AAA', 1), 'BBB': record('BBB', 4), 'CCC': record('CCC', 8)}
    selected = select_candidates([row(s) for s in records], records, config(3), timestamp=NOW, held_symbols=['HELD'])
    assert selected['symbols'] == ['HELD', 'CCC', 'BBB']
    assert selected['held_without_candidate'] == ['HELD']
    selected = select_candidates([row('AAA')], records, config(), timestamp=NOW)
    assert selected['symbols'] == ['AAA'] and not selected['admission']['AAA']['entry_inputs_ready']


def test_drops_expensive_stale_and_unresolved_features():
    expensive = row('AAA'); expensive['last'] = 100
    rows = [expensive, row('BBB')]
    assert not select_candidates(rows, {'AAA': record('AAA')}, config(), timestamp=NOW)['symbols']
    assert not select_candidates([row('AAA')], {'AAA': record('AAA')}, config(), timestamp=NOW + 901)['symbols']
    with pytest.raises(ValueError):
        select_candidates([], {}, config(1), timestamp=NOW, held_symbols=['A', 'B'])


def test_bounded_scan_ignores_bad_rows():
    class Client:
        def hscan(self, *args, **kwargs):
            return 0, {'good': json.dumps(row('AAA')), 'bad': 'not-json', 'wrong': '[]'}
    assert scanner_rows(Client()) == [row('AAA')]
    class Large:
        def hscan(self, *args, **kwargs):
            return 1, {str(i): json.dumps(row(f'S{i}')) for i in range(700)}
    assert len(scanner_rows(Large())) == 500


def test_lookup_uses_exact_ids_not_history_scan():
    class Connection:
        def execute(self, sql, params):
            assert 'id = ANY(%s)' in sql and params == (['AAA'],)
            return self
        def fetchall(self): return [record('AAA')]
    assert load_records(Connection(), [row('AAA'), row('AAA')])['AAA']['symbol'] == 'AAA'
    assert load_records(None, []) == {}


def test_weekend_check_does_not_read_or_train():
    result = check(None, now=NOW + 86400)
    assert result['status'] == 'market_closed' and not result['training_started']
    assert result['broker_calls'] == 0


def test_empty_scanner_waits_without_database_connection():
    class Empty:
        def hscan(self, *args, **kwargs): return 0, {}
    result = check(Empty(), now=NOW, connect=None)
    assert result['status'] == 'waiting_for_scanner'
