"""Read-only market readiness checks, explicitly not an active PPO learner."""
from datetime import datetime
import json
import os
import time
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
import redis

from drl_agent.live_candidates import scanner_rows, load_records, watch_config, select_candidates
from drl_agent.live_inputs import RedisLiveInputs
from drl_agent.live_reward import session_bounds
from drl_agent.live_stream_adapter import LiveStreamAdapter

KEY = 'drl:live:readiness'


def check(client, *, now, connect=psycopg.connect):
    result = dict(ts=now, mode='readiness_only', training_started=False, broker_calls=0,
                  selected_count=0, usable_count=0, errors={})
    day = datetime.fromtimestamp(now, ZoneInfo('America/New_York')).date()
    bounds = session_bounds(day)
    if bounds is None or not bounds[0] <= now < bounds[1]:
        return {**result, 'status': 'market_closed'}
    rows = scanner_rows(client)
    if not rows:
        return {**result, 'status': 'waiting_for_scanner'}
    with connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=3,
                 options='-c default_transaction_read_only=on -c statement_timeout=3000') as connection:
        row = connection.execute("SELECT settings FROM scanner_settings WHERE id = 'default'").fetchone()
        if not row or not isinstance(row['settings'], dict):
            return {**result, 'status': 'settings_unavailable'}
        config = watch_config(row['settings'])
        records = load_records(connection, rows)
    selected = select_candidates(rows, records, config, timestamp=now)
    symbols = selected['symbols']
    if not symbols:
        return {**result, 'status': 'waiting_for_candidates', 'rejected_count': len(selected['rejected'])}
    result['selected_count'] = len(symbols)
    snapshots = RedisLiveInputs(client, symbols).read(now)
    adapter = LiveStreamAdapter(symbols)
    for symbol in symbols:
        try:
            adapter.adapt(snapshots[symbol], timestamp=now, account={}, scanner_record=selected['records'][symbol])
            result['usable_count'] += 1
        except (ValueError, TypeError, KeyError) as exc:
            result['errors'][symbol] = str(exc)
    result['status'] = 'inputs_ready_not_training' if result['usable_count'] else 'waiting_for_market_inputs'
    return result


def main():
    client = redis.from_url(os.environ['REDIS_URL'], decode_responses=True,
                           socket_timeout=3, socket_connect_timeout=3)
    while True:
        now = time.time()
        try:
            result = check(client, now=now)
        except Exception as exc:
            # Do not expose connection strings or credentials in operational status.
            result = dict(ts=now, status='check_failed', error_type=type(exc).__name__,
                          mode='readiness_only', training_started=False, broker_calls=0)
        try:
            client.set(KEY, json.dumps(result), ex=45)
        except redis.RedisError:
            print(json.dumps({'status': 'heartbeat_write_failed'}), flush=True)
        print(json.dumps(result), flush=True)
        time.sleep(15)


if __name__ == '__main__':
    main()
