"""Bounded, read-only snapshots of the historical preparation job."""
import calendar
import json
import os
from pathlib import Path
import re
import time


def status(root=None):
    root = Path(root or os.getenv('DRL_TRAINING_STATUS_ROOT', '/training-status'))
    errors, modified = [], []

    def read(relative):
        path = root / relative
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Outside status root')
            stat = path.stat()
            if stat.st_size > 16 * 1024 * 1024:
                raise ValueError('Oversized snapshot')
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('Invalid snapshot')
            modified.append(stat.st_mtime)
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            errors.append(str(relative))
            return {}

    run = read('history-expansion-v1/progress.json')
    supervisor = read('history-expansion-v1/supervisor.json')
    month = run.get('current_month', '')
    exported, acquired, prepared, assembly, readiness = {}, {}, {}, {}, {}
    if isinstance(month, str) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        year, number = map(int, month.split('-'))
        corpus = f'news-corpus-{calendar.month_name[number].lower()}-{year}-v1'
        exported = read(f'{corpus}/progress.json')
        acquired = read(f'{corpus}/acquisition/progress.json')
        prepared = read(f'{corpus}/preparation-v1/progress.json')
        assembly = read(f'{corpus}/preparation-v1/sessions/progress.json')
        readiness = read(f'{corpus}/preparation-v1/sessions/readiness.json')
    def count(value):
        return len(value) if isinstance(value, (dict, list)) else None
    dates = [r.get('date') for r in exported.get('completed', []) if isinstance(r, dict) and r.get('date')]
    dates.sort()
    completed = acquired.get('completed', {})
    return dict(
        status='snapshot_error' if errors else supervisor.get('status', run.get('status', 'unavailable')),
        stage=run.get('stage'), current_month=month or None,
        target_start=run.get('start'), target_end_exclusive=run.get('end'),
        completed_months=run.get('completed_months', []),
        retry=supervisor.get('retry'), error_type=supervisor.get('error_type') or run.get('error_type'),
        snapshot_errors=errors, snapshot_ts=max(modified) if modified else None,
        snapshot_age_seconds=max(0, int(time.time() - max(modified))) if modified else None,
        export=dict(status=exported.get('status'), days=count(exported.get('completed')),
                    first_date=dates[0] if dates else None, last_date=dates[-1] if dates else None,
                    articles=exported.get('selected_articles'), symbols=exported.get('unique_symbols')),
        acquisition=dict(status=acquired.get('status'), completed=count(acquired.get('completed')),
                         remaining=acquired.get('remaining'), symbol=acquired.get('current_symbol'),
                         no_data=sum(r.get('status') == 'no_data' for r in completed.values()
                                     if isinstance(r, dict)) if isinstance(completed, dict) else None),
        assembly=dict(status=assembly.get('status', prepared.get('status')),
                      completed=count(assembly.get('completed')) if assembly else prepared.get('total_sessions'),
                      total=assembly.get('total_sessions', prepared.get('total_sessions')),
                      remaining=assembly.get('remaining', prepared.get('remaining')),
                      totals=readiness.get('totals')),
        training_started=False, split_certified=False,
    )
