"""Export a bounded observation dataset from PostgreSQL without changing records."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from drl_agent.dataset import build_dataset, encode_dataset


def load_records(symbol: str, *, start_ts: int, end_ts: int, lookback_seconds: int,
                 max_records: int = 100_000) -> tuple[list[dict], list[dict]]:
    if not 1 <= max_records <= 100_000:
        raise ValueError("max_records must be 1..100000")
    params = {"symbol": symbol.upper(), "start": start_ts - lookback_seconds,
              "end": end_ts, "limit": max_records + 1}
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '30s'")
        scanner = conn.execute(
            """SELECT id, symbol, ts, source, created_at, payload FROM feature_store
               WHERE symbol = %(symbol)s AND source = 'scanner_shadow'
                 AND ts BETWEEN %(start)s AND %(end)s
                 AND created_at <= to_timestamp(%(end)s)
               ORDER BY ts, created_at, id LIMIT %(limit)s""", params,
        ).fetchall()
        market = conn.execute(
            """SELECT observation_key, symbol, observation_type, source, event_ts,
                      received_ts, price, bid, ask, open, high, low, close, volume, payload
               FROM market_observations
               WHERE symbol = %(symbol)s AND source = 'schwab_stream'
                 AND observation_type IN ('levelone_quote', 'level2_book', 'minute_candle')
                 AND event_ts BETWEEN %(start)s AND %(end)s
                 AND received_ts <= %(end)s
               ORDER BY event_ts, received_ts, observation_key LIMIT %(limit)s""", params,
        ).fetchall()
    if len(scanner) > max_records or len(market) > max_records:
        raise ValueError("Input limit exceeded; shorten the window rather than truncate records")
    return scanner, market


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start-ts", type=int, required=True)
    parser.add_argument("--end-ts", type=int, required=True)
    parser.add_argument("--step-seconds", type=int, default=10)
    parser.add_argument("--lookback-seconds", type=int, default=900)
    parser.add_argument("--history-limit", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = {"symbol": args.symbol, "start_ts": args.start_ts, "end_ts": args.end_ts,
              "step_seconds": args.step_seconds, "lookback_seconds": args.lookback_seconds,
              "history_limit": args.history_limit}
    build_dataset(**config, scanner_rows=[], market_rows=[])  # Validate before database reads.
    if args.output.exists():
        raise FileExistsError(args.output)
    scanner, market = load_records(args.symbol, start_ts=args.start_ts,
                                  end_ts=args.end_ts, lookback_seconds=args.lookback_seconds)
    result = build_dataset(**config, scanner_rows=scanner, market_rows=market)
    encoded = encode_dataset(result)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(encoded + "\n")
    print(encode_dataset(result["manifest"]))


if __name__ == "__main__":
    main()
