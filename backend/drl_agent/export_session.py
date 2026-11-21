"""Bounded multi-symbol, single-session export; no broker, model, or GPU calls."""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from drl_agent.dataset import _normalize, build_dataset, encode_dataset
from drl_agent.watch_replay import WatchConfig


def validate_window(*, start_ts, end_ts, step_seconds=10, lookback_seconds=900, history_limit=12):
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (start_ts, end_ts, step_seconds)) or step_seconds <= 0:
        raise ValueError("Decision times and positive step must be integers")
    if end_ts <= start_ts or (end_ts - start_ts) % step_seconds:
        raise ValueError("Provide at least two aligned decision times")
    ny = ZoneInfo("America/New_York")
    if datetime.fromtimestamp(start_ts, ny).date() != datetime.fromtimestamp(end_ts, ny).date():
        raise ValueError("Window must stay within one New York session date")
    build_dataset("VALIDATE", start_ts=start_ts, end_ts=end_ts, step_seconds=step_seconds,
                  lookback_seconds=lookback_seconds, history_limit=history_limit,
                  scanner_rows=[], market_rows=[])


def recorded_symbols(rows, end_ts):
    symbols = set()
    for raw in rows:
        try:
            if raw.get("source") == "scanner_shadow" and _normalize(raw, scanner=True)["available_ms"] <= end_ts * 1000:
                symbol = str(raw.get("symbol") or "").strip().upper()
                if symbol:
                    symbols.add(symbol)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return sorted(symbols)


def load_session_records(*, start_ts, end_ts, lookback_seconds=900, max_records=100000):
    if not 1 <= max_records <= 100000:
        raise ValueError("max_records must be 1..100000")
    params = {"start": start_ts - lookback_seconds, "end": end_ts, "limit": max_records + 1}
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '30s'")
        scanner = conn.execute(
            """SELECT id, symbol, ts, source, created_at, payload FROM feature_store
               WHERE source = 'scanner_shadow' AND ts BETWEEN %(start)s AND %(end)s
                 AND created_at <= to_timestamp(%(end)s)
               ORDER BY ts, created_at, id LIMIT %(limit)s""", params).fetchall()
        if len(scanner) > max_records:
            raise ValueError("Input limit exceeded; shorten the window")
        symbols = recorded_symbols(scanner, end_ts)
        if not symbols:
            return scanner, []
        market = conn.execute(
            """SELECT observation_key, symbol, observation_type, source, event_ts,
                      received_ts, price, bid, ask, open, high, low, close, volume, payload
               FROM market_observations
               WHERE symbol = ANY(%(symbols)s) AND source = 'schwab_stream'
                 AND observation_type IN ('levelone_quote', 'level2_book', 'minute_candle')
                 AND event_ts BETWEEN %(start)s AND %(end)s AND received_ts <= %(end)s
               ORDER BY event_ts, received_ts, observation_key LIMIT %(limit)s""",
            {**params, "symbols": symbols, "limit": max_records - len(scanner) + 1}).fetchall()
    if len(scanner) + len(market) > max_records:
        raise ValueError("Input limit exceeded; shorten the window")
    return scanner, market


def export_session(output, *, scanner_rows, market_rows, watch_config, start_ts, end_ts,
                   step_seconds=10, lookback_seconds=900, history_limit=12):
    options = dict(start_ts=start_ts, end_ts=end_ts, step_seconds=step_seconds,
                   lookback_seconds=lookback_seconds, history_limit=history_limit)
    validate_window(**options)
    symbols = recorded_symbols(scanner_rows, end_ts)
    if not symbols:
        raise ValueError("No available Scanner symbols in this window")
    states = (end_ts - start_ts) // step_seconds + 1
    if len(scanner_rows) + len(market_rows) > 100000 or states * len(symbols) * (3 * history_limit + 1) > 250000:
        raise ValueError("Session output budget exceeded; shorten the window or history")
    scanner_by_symbol = {s: [] for s in symbols}
    market_by_symbol = {s: [] for s in symbols}
    for rows, groups in ((scanner_rows, scanner_by_symbol), (market_rows, market_by_symbol)):
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if symbol in groups:
                groups[symbol].append(row)
    output = Path(output)
    output.mkdir(exist_ok=False)
    files, coverage = [], {}
    for symbol in symbols:
        data = build_dataset(symbol, **options, scanner_rows=scanner_by_symbol[symbol],
                             market_rows=market_by_symbol[symbol])
        # Provider symbols are data, never path components.
        filename = "observations-" + hashlib.sha256(symbol.encode()).hexdigest() + ".json"
        with (output / filename).open("x", encoding="utf-8") as stream:
            stream.write(encode_dataset(data) + "\n")
        files.append(filename)
        coverage[symbol] = {"file": filename, "dataset_sha256": data["manifest"]["dataset_sha256"],
                            "coverage_states": data["manifest"]["coverage_states"],
                            "excluded": data["manifest"]["excluded"]}
    manifest = {"version": "drl-session-export-v1", "sessions": [files],
                "watch_config": asdict(watch_config), "window": options,
                "symbol_count": len(symbols), "input_scanner_rows": len(scanner_rows),
                "input_market_rows": len(market_rows), "coverage": coverage,
                "universe": "all_available_recorded_scanner_symbols_including_lookback",
                "training_ready": False, "broker_calls": 0}
    manifest["manifest_sha256"] = hashlib.sha256(encode_dataset(manifest).encode()).hexdigest()
    # Consumers only see a complete manifest after all referenced files exist.
    pending = output / ".manifest.json.tmp"
    with pending.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(manifest) + "\n")
    pending.rename(output / "manifest.json")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("start-ts", "end-ts", "capacity"):
        parser.add_argument("--" + name, type=int, required=True)
    for name in ("minimum-price", "maximum-price", "minimum-rvol", "maximum-float"):
        parser.add_argument("--" + name, type=float, required=True)
    parser.add_argument("--step-seconds", type=int, default=10)
    parser.add_argument("--lookback-seconds", type=int, default=900)
    parser.add_argument("--history-limit", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = WatchConfig(args.minimum_price, args.maximum_price, args.minimum_rvol,
                         args.maximum_float, args.capacity)
    options = {key: getattr(args, key) for key in ("start_ts", "end_ts", "step_seconds", "lookback_seconds", "history_limit")}
    validate_window(**options)
    if args.output.exists():
        raise FileExistsError(args.output)
    scanner, market = load_session_records(start_ts=args.start_ts, end_ts=args.end_ts,
                                           lookback_seconds=args.lookback_seconds)
    result = export_session(args.output, scanner_rows=scanner, market_rows=market,
                            watch_config=config, **options)
    print(encode_dataset({k: v for k, v in result.items() if k not in {"coverage", "sessions"}}))


if __name__ == "__main__":
    main()
