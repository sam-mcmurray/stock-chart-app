"""First-pass monthly assembly; no float lookups or trade-gap downloads."""
from datetime import datetime, timezone
import json
from pathlib import Path

from drl_agent.corpus_gap_audit import audit, calendar
from drl_agent.candidate_gap_plan import build_plan, verify
from drl_agent.candidate_sessions import assemble
from drl_agent.candidate_session_report import report
from drl_agent.historical_corpus import _save
from drl_agent.historical_news_join import _verify
from drl_agent.watch_replay import WatchConfig


def prepare(corpus, cache, price_plan, acquisition, watch_config, *, notify=None):
    corpus, cache = Path(corpus), Path(cache)
    root = corpus / 'preparation-v1'
    root.mkdir(exist_ok=True)
    config_path = root / 'config.json'
    config = dict(watch_config=watch_config, price_plan_sha256=price_plan['plan_sha256'],
                  float_policy='unknown', gap_policy='raw_bars_only_no_gap_downloads')
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise ValueError('Frozen preparation configuration changed')
    _save(config_path, config)
    # Freeze the acquisition receipt: its run timestamps change on downloader resume.
    receipt_path = root / 'acquisition.json'
    if receipt_path.exists():
        frozen = json.loads(receipt_path.read_text())
        if (frozen['plan_sha256'] != acquisition['plan_sha256'] or
                frozen['completed'] != acquisition['completed']):
            raise ValueError('Acquisition evidence changed')
        acquisition = frozen
    else:
        _save(receipt_path, acquisition)
    empty_evidence = root / 'gap-evidence'
    empty_evidence.mkdir(exist_ok=True)
    if list(empty_evidence.iterdir()):
        raise ValueError('First-pass gap evidence must remain empty')
    windows = price_plan['requests'] + price_plan['reusable_archives']
    if not windows:
        result = dict(status='complete', total_sessions=0, remaining=0, training_started=False)
        _save(root / 'progress.json', result)
        return result
    cal_path = root / 'calendar.json'
    if not cal_path.exists():
        first = datetime.fromtimestamp(min(r['start_ts'] for r in windows), timezone.utc).date()
        last = datetime.fromtimestamp(max(r['end_ts'] for r in windows), timezone.utc).date()
        _save(cal_path, calendar(first.isoformat(), last.isoformat()))
    schedule = json.loads(cal_path.read_text())
    audit_dir = root / 'audit'
    while True:
        result = audit(price_plan, acquisition, schedule, cache, empty_evidence, audit_dir, max_symbols=10000)
        if result['status'] == 'complete':
            break
    candidate_path = root / 'candidates.json'
    settings = WatchConfig(**watch_config)
    if not candidate_path.exists():
        _save(candidate_path, build_plan(corpus, audit_dir, cache, schedule, settings))
    candidates = json.loads(candidate_path.read_text())
    _verify(candidates, 'candidate-gap-plan-v1', 'plan_sha256')
    verification = verify(candidates, empty_evidence, root / 'verification', cached_only=True)
    sessions = root / 'sessions'
    while True:
        result = assemble(candidates, corpus, audit_dir, cache, empty_evidence,
                          verification, sessions, settings, max_sessions=1000, notify=notify)
        _save(root / 'progress.json', {k: v for k, v in result.items() if k != 'completed'})
        if result['status'] == 'complete':
            break
    _save(sessions / 'readiness.json', report(sessions))
    return {k: v for k, v in result.items() if k != 'completed'}
