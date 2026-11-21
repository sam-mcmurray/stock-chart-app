from dataclasses import asdict
import json

import pytest

from drl_agent import historical_preparation as module
from test.test_candidate_sessions import inputs


def test_preparation_assembles_unknown_float_without_downloads(monkeypatch, tmp_path):
    candidates, verification, config = inputs(tmp_path)
    raw_audit = (tmp_path / 'ABC.json').read_text()
    def audit(*args, **kwargs):
        target = args[5]
        target.mkdir(exist_ok=True)
        (target / 'ABC.json').write_text(raw_audit)
        return {'status': 'complete'}
    monkeypatch.setattr(module, 'audit', audit)
    monkeypatch.setattr(module, 'calendar', lambda *args: {})
    monkeypatch.setattr(module, 'build_plan', lambda *args: candidates)
    def verify(*args, **kwargs):
        assert kwargs['cached_only'] is True
        return verification
    monkeypatch.setattr(module, 'verify', verify)
    prices = dict(plan_sha256='test', requests=[dict(start_ts=1790000000, end_ts=1790109800)],
                  reusable_archives=[])
    acquired = dict(plan_sha256='test', completed={}, updated_at='first')
    result = module.prepare(tmp_path, tmp_path, prices, acquired, asdict(config))
    assert result['total_sessions'] == 2 and result['status'] == 'complete'
    summary = json.loads((tmp_path / 'preparation-v1/sessions/ABC_2026-09-21/summary.json').read_text())
    assert not summary['float_evidence_supplied']
    assert not summary['training_started'] and summary['network_calls'] == 0
    # Downloader bookkeeping changes must not invalidate the frozen audit.
    result = module.prepare(tmp_path, tmp_path, prices, dict(acquired, updated_at='later'), asdict(config))
    assert result['total_sessions'] == 2
    with pytest.raises(ValueError, match='Acquisition evidence changed'):
        module.prepare(tmp_path, tmp_path, prices, dict(acquired, completed={'new': {}}), asdict(config))


def test_empty_month_preserved(tmp_path):
    result = module.prepare(tmp_path, tmp_path,
        dict(plan_sha256='empty', requests=[], reusable_archives=[]),
        dict(plan_sha256='empty', completed={}), {})
    assert result['total_sessions'] == 0 and result['status'] == 'complete'
