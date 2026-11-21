import pytest

from drl_agent import historical_news


def test_bounded_reads_deduplicate_corrected_identity(monkeypatch):
    calls = []
    def load(**kwargs):
        calls.append(kwargs)
        return [dict(record_type='news', record_id='same', observed_ts=9000,
                     version_key='latest', published_ts=8500)]
    monkeypatch.setattr(historical_news, '_load_version_window', load)
    rows = historical_news.load_versions(start_ts=1, end_ts=8000, enrichment_cutoff_ts=10000)
    assert [(c['start_ts'], c['end_ts']) for c in calls] == [(1, 3601), (3601, 7201), (7201, 8000)]
    assert all(c['enrichment_cutoff_ts'] == 10000 for c in calls)
    assert len(rows) == 1
    # Corrections outside the requested date must not resurrect an older version.
    assert rows[0]['published_ts'] == 8500


def test_global_limit_applies_across_windows(monkeypatch):
    def load(**kwargs):
        return [dict(record_type='news', record_id=str(kwargs['start_ts']),
                     observed_ts=9000, version_key='v')]
    monkeypatch.setattr(historical_news, '_load_version_window', load)
    with pytest.raises(ValueError, match='limit exceeded'):
        historical_news.load_versions(start_ts=1, end_ts=8000, enrichment_cutoff_ts=10000, limit=2)


def test_read_failure_does_not_return_partial_day(monkeypatch):
    def load(**kwargs):
        if kwargs['start_ts'] > 1:
            raise RuntimeError('failed read')
        return []
    monkeypatch.setattr(historical_news, '_load_version_window', load)
    with pytest.raises(RuntimeError, match='failed read'):
        historical_news.load_versions(start_ts=1, end_ts=8000, enrichment_cutoff_ts=10000)


def test_timeout_splits_only_failed_window(monkeypatch):
    calls = []
    def load(**kwargs):
        calls.append((kwargs['start_ts'], kwargs['end_ts']))
        if kwargs['end_ts'] - kwargs['start_ts'] > 1800:
            raise historical_news.psycopg.errors.QueryCanceled('timeout')
        return [dict(record_type='news', record_id=str(kwargs['start_ts']),
                     observed_ts=9000, version_key='v')]
    monkeypatch.setattr(historical_news, '_load_version_window', load)
    rows = historical_news.load_versions(start_ts=1, end_ts=3601, enrichment_cutoff_ts=10000)
    assert calls == [(1, 3601), (1, 1801), (1801, 3601)]
    assert len(rows) == 2


def test_timeout_floor_stops_instead_of_retrying_forever(monkeypatch):
    calls = []
    def load(**kwargs):
        calls.append(kwargs)
        raise historical_news.psycopg.errors.QueryCanceled('timeout')
    monkeypatch.setattr(historical_news, '_load_version_window', load)
    with pytest.raises(historical_news.psycopg.errors.QueryCanceled):
        historical_news.load_versions(start_ts=1, end_ts=61, enrichment_cutoff_ts=10000)
    assert len(calls) == 1
