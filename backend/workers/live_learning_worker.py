"""Local shadow PPO supervisor. No broker credentials or order submission."""
from dataclasses import asdict
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
import redis
import torch

from drl_agent.live_candidates import scanner_rows, load_records, watch_config, select_candidates
from drl_agent.live_coordinator import LiveCoordinator
from drl_agent.live_inputs import RedisLiveInputs
from drl_agent.live_reward import session_bounds

KEY = 'drl:live:learner'


def discovery(client):
    rows = scanner_rows(client)
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=3,
                        options='-c default_transaction_read_only=on -c statement_timeout=3000') as conn:
        saved = conn.execute("SELECT settings FROM scanner_settings WHERE id = 'default'").fetchone()
        if not saved or not isinstance(saved['settings'], dict):
            raise ValueError('Saved scanner settings required')
        config = watch_config(saved['settings'])
        records = load_records(conn, rows)
    return rows, records, config


class Supervisor:
    def __init__(self, client, checkpoint, *, discover=discovery, coordinator_factory=LiveCoordinator,
                 reader_factory=RedisLiveInputs):
        self.client, self.path = client, Path(checkpoint)
        self.discover, self.coordinator_factory, self.reader_factory = discover, coordinator_factory, reader_factory
        self.coordinator = self.reader = None
        self.cached = None
        self.discovered_at = 0
        self.blocked = None
        self.quarantine = {}

    def report(self, now, status, **extra):
        coordinator = self.coordinator
        return dict(ts=now, status=status, mode='shadow_learning', broker_calls=0,
            training_started=bool(coordinator and coordinator.agent.version),
            policy_version=coordinator.agent.version if coordinator else 0,
            process_training_transitions=coordinator.transitions if coordinator else 0,
            active_symbols=list(coordinator.symbols) if coordinator else [],
            quarantined_symbols={s: dict(item) for s, item in self.quarantine.items() if item['retry_at'] > now},
            account_scope='independent_symbol_simulations', **extra)

    def tick(self, now):
        if self.blocked:
            return self.report(now, 'blocked', error_type=self.blocked)
        day = datetime.fromtimestamp(now, ZoneInfo('America/New_York')).date()
        bounds = session_bounds(day)
        if bounds is None or not bounds[0] <= now < bounds[1]:
            if self.coordinator:
                try:
                    self.coordinator.tick(timestamp=now, snapshots={}, scanner_records={})
                except Exception as exc:
                    self.blocked = type(exc).__name__
                    return self.report(now, 'blocked', error_type=self.blocked)
            return self.report(now, 'market_closed')
        try:
            if self.cached is None or not 0 <= now - self.discovered_at < 15:
                self.cached = self.discover(self.client)
                self.discovered_at = now
            rows, records, config = self.cached
            if self.coordinator is None and self.path.exists():
                # Bootstrap only the roster; the coordinator validates the entire
                # checkpoint before accepting weights or account state.
                saved = torch.load(self.path, map_location='cpu', weights_only=True)
                roster = tuple(item['symbol'] for item in saved['executions'])
                self.coordinator = self.coordinator_factory(roster, session_date=day.isoformat(),
                    watch_config=config, checkpoint=self.path)
            if self.coordinator and asdict(config) != asdict(self.coordinator.watch_config):
                self.blocked = 'ScannerSettingsChanged'
                return self.report(now, 'blocked', error_type=self.blocked)
            held = [e.symbol for e in self.coordinator.executions if e.quantity] if self.coordinator else []
            selected = select_candidates(rows, records, config, timestamp=now, held_symbols=held)
            desired = set(selected['symbols'])
            self.quarantine = {s: item for s, item in self.quarantine.items() if s in desired and s not in held}
            allowed = {s for s in desired if s in held or self.quarantine.get(s, {}).get('retry_at', 0) <= now}
            # Additions wait for a completed window, so a repeatedly bad newcomer
            # cannot reset all healthy trajectories before they reach an update.
            if self.coordinator and self.coordinator.collector.pending is not None:
                continuing = allowed.intersection(self.coordinator.symbols)
                if continuing:
                    allowed = continuing
            symbols = tuple(sorted(allowed))
            if not symbols:
                if self.coordinator:
                    self.coordinator.collector.reset('no_candidates')
                    for execution in self.coordinator.executions:
                        if execution.pending and not execution.pending['protective']:
                            execution.pending = None
                return self.report(now, 'waiting_for_candidate_recovery' if desired else 'waiting_for_candidates')
            if self.coordinator is None:
                self.coordinator = self.coordinator_factory(symbols, session_date=day.isoformat(),
                    watch_config=config, checkpoint=self.path)
                self.coordinator._save()
            elif symbols != self.coordinator.symbols:
                self.coordinator.reconfigure(symbols, session_date=day.isoformat())
            if self.reader is None or tuple(self.reader.symbols) != symbols:
                self.reader = self.reader_factory(self.client, symbols)
            snapshots = self.reader.read(now)
            result = self.coordinator.tick(timestamp=now, snapshots=snapshots, scanner_records=selected['records'])
            held_after = {e.symbol for e in self.coordinator.executions if e.quantity}
            errors = result.get('errors', {})
            for symbol in symbols:
                if symbol in held_after:
                    self.quarantine.pop(symbol, None)
                elif symbol in errors:
                    attempts = self.quarantine.get(symbol, {}).get('attempts', 0) + 1
                    self.quarantine[symbol] = dict(reason=errors[symbol], attempts=attempts,
                        retry_at=now + min(300, 30 * 2 ** min(attempts - 1, 4)))
                else:
                    self.quarantine.pop(symbol, None)
            detail = {key: result[key] for key in ('errors', 'update') if key in result}
            return self.report(now, result['status'], **detail)
        except (redis.RedisError, psycopg.OperationalError) as exc:
            self.cached = None
            if self.coordinator:
                self.coordinator.collector.reset('source_unavailable')
                for execution in self.coordinator.executions:
                    if execution.pending and not execution.pending['protective']:
                        execution.pending = None
            return self.report(now, 'source_unavailable', error_type=type(exc).__name__)
        except Exception as exc:
            self.blocked = type(exc).__name__
            return self.report(now, 'blocked', error_type=self.blocked)


def main():
    directory = Path(os.getenv('LIVE_LEARNING_PATH', '/training/live-level2-v1'))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = redis.from_url(os.environ['REDIS_URL'], decode_responses=True,
                               socket_timeout=3, socket_connect_timeout=3)
        supervisor = Supervisor(client, directory / 'checkpoint.pt')
        last_log = 0
        while True:
            started = time.monotonic()
            now = time.time()
            result = supervisor.tick(now)
            try:
                client.set(KEY, json.dumps(result), ex=45)
            except redis.RedisError:
                result = {**result, 'heartbeat_error': True}
            if now - last_log >= 15 or result['status'] == 'updated':
                print(json.dumps(result), flush=True)
                last_log = now
            interval = 15 if result['status'] in {'market_closed', 'blocked'} else 1
            time.sleep(max(.01, interval - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
