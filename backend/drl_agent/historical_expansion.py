"""Resume monthly news exports and raw-price acquisition, newest month first."""
import argparse
from datetime import date, datetime, timezone
import fcntl
import json
import os
import signal
import time
from pathlib import Path

from dotenv import dotenv_values

from drl_agent.dataset import encode_dataset
from drl_agent.historical_corpus import export_corpus, _save
from drl_agent.historical_corpus_prices import plan
from drl_agent.historical_news_join import _verify
from drl_agent.historical_price_acquisition import acquire


def months(start, end):
    first, stop = date.fromisoformat(start), date.fromisoformat(end)
    if first.day != 1 or stop.day != 1 or first >= stop or (stop - first).days > 18263:
        raise ValueError('Require increasing month boundaries, at most 50 years')
    result = []
    while stop > first:
        previous = date(stop.year - (stop.month == 1), (stop.month - 2) % 12 + 1, 1)
        result.append((previous.isoformat(), stop.isoformat()))
        stop = previous
    return result


def expand(*, start, end, enrichment_cutoff_ts, output, corpus_root, cache, key, secret,
           notify=None, watch_config=None):
    windows = months(start, end)
    output, corpus_root, cache = Path(output), Path(corpus_root), Path(cache)
    config = dict(version='historical-expansion-v1', start=start, end=end,
                  enrichment_cutoff_ts=enrichment_cutoff_ts,
                  corpus_root=str(corpus_root), cache=str(cache))
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.expansion.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config_path = output / 'config.json'
        if config_path.exists() and json.loads(config_path.read_text()) != config:
            raise ValueError('Frozen expansion configuration changed')
        _save(config_path, config)
        if watch_config is not None:
            assembly_path = output / 'assembly-config.json'
            if assembly_path.exists() and json.loads(assembly_path.read_text()) != watch_config:
                raise ValueError('Frozen expansion watch settings changed')
            _save(assembly_path, watch_config)
        progress = dict(config, status='running', completed_months=[], training_started=False)

        def save(stage, **fields):
            progress.update(stage=stage, updated_at=datetime.now(timezone.utc).isoformat(), **fields)
            _save(output / 'progress.json', progress)
            if notify:
                notify(progress)

        try:
            for first, last in windows:
                day = date.fromisoformat(first)
                corpus = corpus_root / f'news-corpus-{day.strftime("%B").lower()}-{day.year}-v1'
                save('news_export', current_month=first[:7], corpus=str(corpus))
                exported = export_corpus(corpus, start=first, end=last,
                    enrichment_cutoff_ts=enrichment_cutoff_ts, max_days=31, notify=notify)
                if exported['status'] != 'complete':
                    raise RuntimeError('Monthly export incomplete')
                save('price_plan', exported_days=len(exported['completed']),
                     selected_articles=exported['selected_articles'], unique_symbols=exported['unique_symbols'])
                plan_path = corpus / 'price-plan.json'
                if plan_path.exists():
                    price_plan = json.loads(plan_path.read_text())
                    _verify(price_plan, 'historical-corpus-price-plan-v1', 'plan_sha256')
                    if (price_plan['enrichment_cutoff_ts'] != enrichment_cutoff_ts or
                            price_plan['source_shards'] != [r['manifest_sha256'] for r in exported['completed']]):
                        raise ValueError('Price plan does not match exported corpus')
                else:
                    price_plan = plan(corpus, cache)
                    _save(plan_path, price_plan)
                save('price_acquisition', planned_requests=len(price_plan['requests']))
                while True:
                    acquired = acquire(price_plan, cache, corpus / 'acquisition',
                        max_requests=10000, key=key, secret=secret, notify=notify)
                    if acquired['status'] == 'complete':
                        break
                    if acquired['status'] != 'paused_at_batch_limit':
                        raise RuntimeError('Price acquisition did not complete')
                if watch_config is not None:
                    from drl_agent.historical_preparation import prepare
                    save('session_assembly')
                    prepared = prepare(corpus, cache, price_plan, acquired, watch_config, notify=notify)
                    save('session_assembly_complete', assembled_sessions=prepared['total_sessions'])
                progress['completed_months'].append(first[:7])
                save('month_complete')
            save('complete', status='complete')
        except Exception as exc:
            save(progress.get('stage', 'unknown'), status='failed', error_type=type(exc).__name__)
            raise
    return progress


def transient_failure(exc, output):
    import psycopg
    if isinstance(exc, (psycopg.OperationalError, psycopg.errors.QueryCanceled)):
        return True
    if type(exc) is not RuntimeError:
        return False
    try:
        progress = json.loads((Path(output) / 'progress.json').read_text())
        if progress['stage'] != 'price_acquisition':
            return False
        receipt = json.loads((Path(progress['corpus']) / 'acquisition/progress.json').read_text())
        return (receipt.get('http_status') in {408, 429, 500, 502, 503, 504}
                or receipt.get('error_type') in {'ConnectionError', 'Timeout', 'ReadTimeout', 'ConnectTimeout'})
    except (OSError, ValueError, KeyError):
        return False


def supervise(options, *, run=expand, pause=time.sleep, idle=signal.pause):
    status_path = Path(options['output']) / 'supervisor.json'
    status_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            _save(status_path, dict(status='running', retry=attempt))
            run(**options)
            _save(status_path, dict(status='complete', retry=attempt))
            break
        except Exception as exc:
            if attempt < 3 and transient_failure(exc, options['output']):
                delay = 30 * 2 ** attempt
                _save(status_path, dict(status='retry_wait', retry=attempt + 1,
                                       delay_seconds=delay, error_type=type(exc).__name__))
                pause(delay)
                continue
            _save(status_path, dict(status='blocked', retry=attempt, error_type=type(exc).__name__))
            break
    # Keep the managed service idle after completion or a permanent failure.
    # Docker can resume it on host reboot without creating an error restart loop.
    while True:
        idle()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('start', 'end'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--enrichment-cutoff-ts', required=True, type=int)
    parser.add_argument('--watch-config', type=Path, help='Enable first-pass assembly with this frozen watch_config')
    parser.add_argument('--supervise', action='store_true')
    for name in ('output', 'corpus-root', 'cache'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    options = vars(args)
    managed = options.pop('supervise')
    options['watch_config'] = json.loads(args.watch_config.read_text())['watch_config'] if args.watch_config else None
    env = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
    options.update(key=os.getenv('APCA_API_KEY_ID') or env.get('APCA_API_KEY_ID'),
                   secret=os.getenv('APCA_API_SECRET_KEY') or env.get('APCA_API_SECRET_KEY'),
                   notify=lambda row: print(encode_dataset(row), flush=True))
    if managed:
        supervise(options)
    else:
        expand(**options)


if __name__ == '__main__':
    main()
