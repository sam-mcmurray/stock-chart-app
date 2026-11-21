import hashlib
import json

import pytest

from drl_agent import historical_expansion as module
from drl_agent.dataset import encode_dataset


def test_months_newest_first():
    assert module.months('2025-12-01', '2026-03-01') == [
        ('2026-02-01', '2026-03-01'), ('2026-01-01', '2026-02-01'),
        ('2025-12-01', '2026-01-01')]
    with pytest.raises(ValueError):
        module.months('2026-01-02', '2026-02-01')


def setup_pipeline(monkeypatch, tmp_path):
    calls = []
    def export(path, **kwargs):
        path.mkdir(parents=True, exist_ok=True)
        calls.append(('export', kwargs['start']))
        return dict(status='complete', completed=[dict(manifest_sha256=kwargs['start'])],
                    selected_articles=1, unique_symbols=1)
    def plan(path, cache):
        start = calls[-1][1]
        calls.append(('plan', start))
        result = dict(version='historical-corpus-price-plan-v1', source_shards=[start],
                      enrichment_cutoff_ts=1800000000, requests=[])
        result['plan_sha256'] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
        return result
    monkeypatch.setattr(module, 'export_corpus', export)
    monkeypatch.setattr(module, 'plan', plan)
    monkeypatch.setattr(module, 'acquire', lambda *a, **k: dict(status='complete'))
    options = dict(start='2026-05-01', end='2026-07-01', enrichment_cutoff_ts=1800000000,
                   output=tmp_path / 'run', corpus_root=tmp_path / 'corpus',
                   cache=tmp_path / 'prices', key='test', secret='test')
    return calls, options


def test_resume_reuses_frozen_plans(monkeypatch, tmp_path):
    calls, options = setup_pipeline(monkeypatch, tmp_path)
    result = module.expand(**options)
    assert result['completed_months'] == ['2026-06', '2026-05']
    assert result['status'] == 'complete'
    assert result['training_started'] is False
    module.expand(**options)
    assert len([c for c in calls if c[0] == 'plan']) == 2
    with pytest.raises(ValueError, match='configuration changed'):
        module.expand(**dict(options, end='2026-08-01'))


def test_failure_stops_and_records_stage(monkeypatch, tmp_path):
    calls, options = setup_pipeline(monkeypatch, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError('provider failed')
    monkeypatch.setattr(module, 'acquire', fail)
    with pytest.raises(RuntimeError):
        module.expand(**options)
    progress = json.loads((options['output'] / 'progress.json').read_text())
    assert progress['stage'] == 'price_acquisition'
    assert progress['status'] == 'failed'
    assert progress['completed_months'] == []
    assert ('export', '2026-05-01') not in calls


def test_acquisition_batch_limit_continues(monkeypatch, tmp_path):
    _, options = setup_pipeline(monkeypatch, tmp_path)
    statuses = iter(['paused_at_batch_limit', 'complete', 'complete'])
    monkeypatch.setattr(module, 'acquire', lambda *a, **k: dict(status=next(statuses)))
    assert module.expand(**options)['status'] == 'complete'


def test_assembly_runs_before_next_month_and_freezes_settings(monkeypatch, tmp_path):
    from drl_agent import historical_preparation
    calls, options = setup_pipeline(monkeypatch, tmp_path)
    def prepare(*args, **kwargs):
        calls.append(('assemble', args[0].name))
        return {'total_sessions': 5}
    monkeypatch.setattr(historical_preparation, 'prepare', prepare)
    module.expand(**options, watch_config={'maximum_price': 20})
    assert calls[2] == ('assemble', 'news-corpus-june-2026-v1')
    with pytest.raises(ValueError, match='watch settings'):
        module.expand(**options, watch_config={'maximum_price': 30})


def test_supervisor_bounds_retries_and_does_not_restart_validation_failure(tmp_path):
    import psycopg
    options = {'output': tmp_path}
    waits, calls = [], []
    def run(**kwargs):
        calls.append(1)
        raise psycopg.OperationalError('temporary')
    def idle():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        module.supervise(options, run=run, pause=waits.append, idle=idle)
    assert len(calls) == 4 and waits == [30, 60, 120]
    assert json.loads((tmp_path / 'supervisor.json').read_text())['status'] == 'blocked'
    def invalid(**kwargs):
        raise ValueError('invalid artifact')
    waits.clear()
    with pytest.raises(KeyboardInterrupt):
        module.supervise(options, run=invalid, pause=waits.append, idle=idle)
    assert not waits


def test_provider_unauthorized_is_not_transient(tmp_path):
    corpus = tmp_path / 'month'
    (corpus / 'acquisition').mkdir(parents=True)
    (tmp_path / 'progress.json').write_text(json.dumps({'stage': 'price_acquisition', 'corpus': str(corpus)}))
    receipt = corpus / 'acquisition/progress.json'
    receipt.write_text(json.dumps({'http_status': 401}))
    assert not module.transient_failure(RuntimeError(), tmp_path)
    receipt.write_text(json.dumps({'http_status': 429}))
    assert module.transient_failure(RuntimeError(), tmp_path)
