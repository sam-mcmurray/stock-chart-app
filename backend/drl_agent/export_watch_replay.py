"""Read-only, bounded multi-symbol scanner replay export from PostgreSQL."""
import argparse
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from drl_agent.dataset import encode_dataset
from drl_agent.watch_replay import WatchConfig, replay_watch_windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-ts", type=int, required=True)
    parser.add_argument("--end-ts", type=int, required=True)
    parser.add_argument("--step-seconds", type=int, default=10)
    parser.add_argument("--minimum-price", type=float, required=True)
    parser.add_argument("--maximum-price", type=float, required=True)
    parser.add_argument("--minimum-rvol", type=float, required=True)
    parser.add_argument("--maximum-float", type=float, required=True)
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--watch-window-seconds", type=int, default=3600)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = WatchConfig(args.minimum_price, args.maximum_price, args.minimum_rvol,
                         args.maximum_float, args.capacity,
                         watch_window_seconds=args.watch_window_seconds)
    options = dict(start_ts=args.start_ts, end_ts=args.end_ts,
                   step_seconds=args.step_seconds, config=config)
    replay_watch_windows([], **options)
    if args.output.exists():
        raise FileExistsError(args.output)
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '30s'")
        rows = conn.execute(
            """SELECT id, symbol, ts, source, created_at, payload FROM feature_store
               WHERE source = 'scanner_shadow' AND ts BETWEEN %s AND %s
                 AND created_at <= to_timestamp(%s)
               ORDER BY ts, created_at, id LIMIT 100001""",
            (args.start_ts - config.snapshot_max_age_seconds, args.end_ts, args.end_ts),
        ).fetchall()
    report = replay_watch_windows(rows, **options)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(encode_dataset(report) + "\n")
    print(encode_dataset(report["manifest"]))


if __name__ == "__main__":
    main()
