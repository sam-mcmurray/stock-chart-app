from datetime import datetime, timezone
from dataclasses import replace
from copy import deepcopy

import pytest
import torch

from drl_agent.live_coordinator import LiveCoordinator
from drl_agent.watch_replay import WatchConfig
from services.market_stream import normalize_levelone_equity, normalize_chart_equity, normalize_equity_book

NOW = datetime(2026, 11, 20, 16, tzinfo=timezone.utc).timestamp()


def inputs(timestamp):
    ms = int(timestamp * 1000)
    quote = normalize_levelone_equity(dict(key='ABC', delayed=False,
        **{'1': 5., '2': 5.01, '3': 5., '4': 10, '5': 10, '13': 'Q',
           '32': 'Normal', '34': ms, '35': ms, '37': ms, '38': ms}), received_at_ms=ms)
    book = normalize_equity_book(dict(key='ABC', **{'1': ms,
        '2': [{'0': 5., '1': 1000}], '3': [{'0': 5.01, '1': 1000}]}),
        service='NASDAQ_BOOK', received_at_ms=ms)
    candles = [normalize_chart_equity(dict(key='ABC', **{'2': 5., '3': 5.1, '4': 4.9,
        '5': 5., '6': 10000., '7': int((NOW - n * 60) * 1000)}),
        received_at_ms=int((NOW - n * 60 + 59) * 1000)) for n in range(35, 0, -1)]
    scanner = dict(id='fixture', symbol='ABC', source='scanner_shadow', ts=NOW,
        created_at=datetime.fromtimestamp(NOW, timezone.utc).isoformat(), payload=dict(
            watch_inputs=dict(version='scanner-watch-inputs-v1', observed_at=NOW,
                              price=5., rvol=4., float_shares=1_000_000),
            semantic_signal={'hype': {'evidence_article_ids': ['fixture-article'], 'as_of_ts': NOW}}))
    return dict(timestamp=timestamp, snapshots={'ABC': dict(symbol='ABC', levelone=quote, book=book, candles=candles)},
                scanner_records={'ABC': scanner})


def coordinator(tmp_path):
    return LiveCoordinator(['ABC'], session_date='2026-11-20', watch_config=WatchConfig(1, 20, 3, 20_000_000, 50),
                           checkpoint=tmp_path / 'checkpoint.pt', max_steps=2)


def test_full_coordinator_update_checkpoint_and_restore(tmp_path):
    torch.manual_seed(3)
    run = coordinator(tmp_path)
    before = [p.clone() for p in run.agent.policy.parameters()]
    assert run.tick(**inputs(NOW))['status'] == 'collecting'
    run.tick(**inputs(NOW + 1))
    result = run.tick(**inputs(NOW + 2))
    assert result['status'] == 'updated' and result['policy_version'] == 1
    assert result['process_training_transitions'] == 2 and result['broker_calls'] == 0
    assert any(not torch.equal(a, b) for a, b in zip(before, run.agent.policy.parameters()))
    restored = coordinator(tmp_path)
    assert restored.agent.version == 1
    assert all(torch.equal(a, b) for a, b in zip(restored.agent.policy.parameters(), run.agent.policy.parameters()))
    assert restored.collector.pending is None


def test_weekend_missing_scanner_and_stale_quotes_do_not_train(tmp_path):
    run = coordinator(tmp_path)
    assert run.tick(**inputs(NOW))['status'] == 'collecting'
    data = inputs(NOW + 1); data['scanner_records'] = {}
    assert run.tick(**data)['status'] == 'waiting_for_inputs'
    assert run.collector.pending is None and run.agent.version == 0
    data = inputs(NOW + 2); data['snapshots']['ABC']['levelone']['bid_time_ms'] = int((NOW - 10) * 1000)
    assert run.tick(**data)['status'] == 'waiting_for_inputs'
    assert run.tick(**inputs(NOW + 86400))['status'] == 'market_closed'
    assert run.agent.version == 0


def test_missing_entry_evidence_cancels_prior_pending_buy(tmp_path):
    run = coordinator(tmp_path)
    with torch.no_grad():
        run.agent.policy.actor.weight.zero_()
        run.agent.policy.actor.bias.copy_(torch.tensor([-100., -100., -100., 100.]))
    run.tick(**inputs(NOW))
    assert run.executions[0].pending['target'] > 0
    data = inputs(NOW + 1); data['scanner_records'] = {}
    run.tick(**data)
    assert run.executions[0].quantity == 0 and run.executions[0].pending is None


def test_checkpoint_failure_blocks_future_learning(tmp_path, monkeypatch):
    run = coordinator(tmp_path)
    def fail(*args, **kwargs): raise OSError('disk full')
    monkeypatch.setattr('drl_agent.live_coordinator.save_checkpoint', fail)
    with pytest.raises(OSError):
        for offset in range(3):
            run.tick(**inputs(NOW + offset))
    assert run.blocked
    assert run.tick(**inputs(NOW + 4))['status'] == 'blocked_error'


def test_removed_flat_account_returns_with_losses_and_peak_intact(tmp_path):
    run = coordinator(tmp_path)
    with torch.no_grad():
        run.agent.policy.actor.weight.zero_()
        run.agent.policy.actor.bias.copy_(torch.tensor([-100., -100., -100., 100.]))
    run.tick(**inputs(NOW)); run.tick(**inputs(NOW + 1))
    execution = run.executions[0]
    execution.propose(0)
    q = execution.latest
    execution.advance(replace(q, timestamp=NOW + 2, event_ts=NOW + 2, received_ts=NOW + 2,
                              book_event_ts=NOW + 2, book_received_ts=NOW + 2))
    cash, purchased, realized = execution.cash, execution.purchased, execution.realized
    assert execution.quantity == 0 and cash < 500
    weights = {k: v.clone() for k, v in run.agent.policy.state_dict().items()}
    assert run.reconfigure(['DEF'], session_date='2026-11-20')
    assert run.inactive_accounts['ABC']['execution'].cash == cash
    assert run.collector.pending is None
    recovered = LiveCoordinator(['DEF'], session_date='2026-11-20',
        watch_config=run.watch_config, checkpoint=run.path)
    assert recovered.reconfigure(['ABC'], session_date='2026-11-20')
    returned = recovered.executions[0]
    assert (returned.cash, returned.purchased, returned.realized) == (cash, purchased, realized)
    assert float(recovered.collector.peak_equity[0]) == 500
    assert returned.latest is None and returned.pending is None
    assert all(torch.equal(value, recovered.agent.policy.state_dict()[key]) for key, value in weights.items())


def test_held_symbols_cannot_be_removed_and_reorder_resets_memory(tmp_path):
    run = coordinator(tmp_path)
    with torch.no_grad():
        run.agent.policy.actor.weight.zero_()
        run.agent.policy.actor.bias.copy_(torch.tensor([-100., -100., -100., 100.]))
    run.tick(**inputs(NOW)); run.tick(**inputs(NOW + 1))
    quantity = run.executions[0].quantity
    assert quantity > 0
    with pytest.raises(ValueError, match='held'):
        run.reconfigure(['DEF'], session_date='2026-11-20')
    assert run.reconfigure(['DEF', 'ABC'], session_date='2026-11-20')
    assert run.executions[1].symbol == 'ABC' and run.executions[1].quantity == quantity
    assert run.collector.pending is None and not run.collector.rows
    assert all(not memory.any() for memory in run.collector.hidden)


def test_failed_roster_checkpoint_leaves_original_mapping_and_file(tmp_path, monkeypatch):
    run = coordinator(tmp_path)
    run._save()
    original = run.path.read_bytes()
    collector, execution = run.collector, run.executions[0]
    def fail(*args, **kwargs): raise OSError('disk full')
    monkeypatch.setattr('drl_agent.live_coordinator.save_checkpoint', fail)
    with pytest.raises(OSError):
        run.reconfigure(['DEF'], session_date='2026-11-20')
    assert run.symbols == ('ABC',) and not run.inactive_accounts
    assert run.collector is collector and run.executions[0] is execution
    assert run.path.read_bytes() == original


def test_supervisor_runs_real_update_and_resumes_checkpoint(tmp_path):
    from workers.live_learning_worker import Supervisor
    config = WatchConfig(1, 20, 3, 20_000_000, 50, watch_window_seconds=604800)
    record = inputs(NOW)['scanner_records']['ABC']
    calls = []
    def discover(client):
        calls.append(True)
        return ([dict(symbol='ABC', ts=NOW, last=5., shadow_feature_id='fixture')], {'fixture': record}, config)
    class Reader:
        def __init__(self, client, symbols): self.symbols = symbols
        def read(self, now): return inputs(now)['snapshots']
    def factory(*args, **kwargs): return LiveCoordinator(*args, **kwargs, max_steps=2)
    options = dict(discover=discover, reader_factory=Reader, coordinator_factory=factory)
    supervisor = Supervisor(None, tmp_path / 'supervised.pt', **options)
    assert supervisor.tick(NOW)['status'] == 'collecting'
    supervisor.tick(NOW + 1)
    result = supervisor.tick(NOW + 2)
    assert result['status'] == 'updated' and result['training_started']
    assert result['policy_version'] == 1 and len(calls) == 1
    resumed = Supervisor(None, tmp_path / 'supervised.pt', **options)
    assert resumed.tick(NOW + 3)['policy_version'] == 1
    assert resumed.coordinator.collector.pending is not None


def test_supervisor_weekend_and_missing_candidates_do_not_train(tmp_path):
    from workers.live_learning_worker import Supervisor
    config = WatchConfig(1, 20, 3, 20_000_000, 50)
    calls = []
    def discover(client):
        calls.append(True)
        return [], {}, config
    supervisor = Supervisor(None, tmp_path / 'empty.pt', discover=discover)
    assert supervisor.tick(NOW + 86400)['status'] == 'market_closed'
    assert not calls
    result = supervisor.tick(NOW)
    assert result['status'] == 'waiting_for_candidates' and not result['training_started']
    assert not supervisor.path.exists()


def test_supervisor_source_outage_retries_but_corrupt_checkpoint_blocks(tmp_path):
    from workers.live_learning_worker import Supervisor
    import redis
    def offline(client): raise redis.ConnectionError('offline')
    supervisor = Supervisor(None, tmp_path / 'bad.pt', discover=offline)
    assert supervisor.tick(NOW)['status'] == 'source_unavailable' and not supervisor.blocked
    supervisor.path.write_bytes(b'not a checkpoint')
    supervisor.discover = lambda client: ([], {}, WatchConfig(1, 20, 3, 20_000_000, 50))
    assert supervisor.tick(NOW + 1)['status'] == 'blocked'
    assert supervisor.tick(NOW + 2)['status'] == 'blocked'


def multi_inputs(now):
    combined = inputs(now)
    other = deepcopy(combined['snapshots']['ABC'])
    other['symbol'] = other['levelone']['symbol'] = other['book']['symbol'] = 'DEF'
    for candle in other['candles']:
        candle['symbol'] = 'DEF'
    record = deepcopy(combined['scanner_records']['ABC'])
    record.update(symbol='DEF', id='def-fixture')
    combined['snapshots']['DEF'] = other
    combined['scanner_records']['DEF'] = record
    return combined


def supervisor_fixture(tmp_path, *, steps, action):
    from workers.live_learning_worker import Supervisor
    config = WatchConfig(1, 20, 3, 20_000_000, 50, watch_window_seconds=604800)
    records = multi_inputs(NOW)['scanner_records']
    rows = [dict(symbol=s, ts=NOW, last=5., shadow_feature_id=r['id']) for s, r in records.items()]
    bad = {'DEF': False}
    class Reader:
        def __init__(self, client, symbols): self.symbols = symbols
        def read(self, now):
            snapshots = multi_inputs(now)['snapshots']
            if bad['DEF']:
                snapshots['DEF']['book'] = None
            return {s: snapshots[s] for s in self.symbols}
    def factory(*args, **kwargs):
        run = LiveCoordinator(*args, **kwargs, max_steps=steps)
        with torch.no_grad():
            run.agent.policy.actor.weight.zero_()
            run.agent.policy.actor.bias.fill_(-100.)
            run.agent.policy.actor.bias[action] = 100.
        return run
    supervisor = Supervisor(None, tmp_path / 'availability.pt',
        discover=lambda client: (rows, {r['id']: r for r in records.values()}, config),
        reader_factory=Reader, coordinator_factory=factory)
    return supervisor, bad


def test_bad_flat_candidate_does_not_starve_healthy_batch_and_recovers_at_boundary(tmp_path):
    supervisor, bad = supervisor_fixture(tmp_path, steps=40, action=0)
    bad['DEF'] = True
    first = supervisor.tick(NOW)
    assert first['status'] == 'waiting_for_inputs'
    assert first['quarantined_symbols']['DEF']['reason'] == 'book_unavailable'
    assert supervisor.tick(NOW + 1)['active_symbols'] == ['ABC']
    for second in range(2, 41):
        result = supervisor.tick(NOW + second)
        assert result['active_symbols'] == ['ABC']
    result = supervisor.tick(NOW + 41)
    assert result['status'] == 'updated' and result['policy_version'] == 1
    bad['DEF'] = False
    result = supervisor.tick(NOW + 42)
    assert result['active_symbols'] == ['ABC', 'DEF'] and not result['quarantined_symbols']
    assert supervisor.coordinator.executions[1].cash == 500


def test_stale_held_symbol_is_not_quarantined_or_dropped(tmp_path):
    supervisor, bad = supervisor_fixture(tmp_path, steps=8, action=3)
    supervisor.tick(NOW); supervisor.tick(NOW + 1)
    held = {e.symbol: e.quantity for e in supervisor.coordinator.executions}
    assert held['DEF'] > 0
    bad['DEF'] = True
    result = supervisor.tick(NOW + 2)
    assert result['status'] == 'waiting_for_inputs'
    assert 'DEF' not in result['quarantined_symbols']
    result = supervisor.tick(NOW + 3)
    assert 'DEF' in result['active_symbols']
    assert next(e for e in supervisor.coordinator.executions if e.symbol == 'DEF').quantity == held['DEF']


def test_repeated_bad_candidate_backs_off_without_resetting_healthy_progress(tmp_path):
    supervisor, bad = supervisor_fixture(tmp_path, steps=2, action=0)
    bad['DEF'] = True
    supervisor.tick(NOW)
    for second in range(1, 34):
        result = supervisor.tick(NOW + second)
    item = supervisor.quarantine['DEF']
    assert item['attempts'] == 2 and item['retry_at'] >= NOW + 90
    assert result['policy_version'] >= 9
