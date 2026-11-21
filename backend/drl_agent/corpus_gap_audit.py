"""Resumable, network-free session-gap inventory for a completed acquisition."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path

from drl_agent.alpaca_data_cache import _time
from drl_agent.alpaca_replay_export import build_export, merge_exports
from drl_agent.dataset import encode_dataset
from drl_agent.historical_corpus_prices import subtract
from drl_agent.historical_news_join import _verify
from drl_agent.trade_volume_evidence import volume_minutes


def signed(value, key):
    value[key] = hashlib.sha256(encode_dataset(value).encode()).hexdigest()
    return value


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(encode_dataset(value) + "\n")
    temporary.replace(path)


def calendar(start, end):
    import exchange_calendars as xcals
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("Invalid calendar range")
    cal = xcals.get_calendar("XNYS", start=first-timedelta(days=7), end=last+timedelta(days=7))
    return signed({"version": "historical-gap-calendar-v1", "start": start, "end": end,
                   "sessions": [{"date": str(s.date()), "open_ts": int(cal.session_open(s).timestamp()),
                                 "close_ts": int(cal.session_close(s).timestamp())}
                                for s in cal.sessions_in_range(start, end)]}, "calendar_sha256")


def audit_symbol(symbol, sources, intervals, schedule, cache, evidence):
    exports = []
    for row in sources:
        path = Path(row["file"])
        if path.name != str(path):
            raise ValueError("Invalid archive filename")
        raw = json.loads((cache / path).read_text())
        if raw["archive_sha256"] != row["archive_sha256"] or raw["request"]["symbols"] != symbol:
            raise ValueError("Changed acquisition archive")
        exports.append(build_export(raw))
    candles = merge_exports(exports)["candles"] if exports else []
    times = {c["start_ts"] for c in candles}
    gaps, sessions = [], []
    for session in schedule["sessions"]:
        low, high = session["open_ts"], session["close_ts"]
        if subtract([(low, high)], intervals):
            continue
        missing = [t for t in range(low, high, 60) if t not in times]
        gaps.extend(missing)
        sessions.append({**session, "missing_price_minutes": len(missing), "expected_minutes": (high-low)//60})
    verified, unsupported, uncached = [], [], []
    for stamp in sorted(gaps, reverse=True):
        filename = f"{symbol}_{datetime.fromtimestamp(stamp, timezone.utc):%Y%m%dT%H%MZ}_sip_trades.json"
        artifact = evidence.get(filename)
        if artifact is None:
            uncached.append(stamp)
            continue
        try:
            value = volume_minutes([artifact], symbol)
        except ValueError as exc:
            if str(exc) != "Unsupported volume evidence trade conditions":
                raise
            unsupported.append(stamp)
            continue
        if set(value) != {stamp}:
            raise ValueError("Trade evidence filename/time mismatch")
        verified.append(stamp)
    return signed({"version": "historical-symbol-gap-audit-v1", "symbol": symbol,
        "source_archives": sources, "sessions": sessions, "price_bars": len(candles),
        "missing_price_minutes": len(gaps), "verified_volume_minutes": verified,
        "unsupported_condition_minutes": unsupported, "uncached_minutes": uncached,
        "training_ready": False, "float_evidence_joined": False, "network_calls": 0}, "audit_sha256")


def audit(plan, acquisition, schedule, cache, evidence_dir, output, *, max_symbols=20):
    _verify(plan, "historical-corpus-price-plan-v1", "plan_sha256")
    _verify(schedule, "historical-gap-calendar-v1", "calendar_sha256")
    if (acquisition.get("status") != "complete" or acquisition["plan_sha256"] != plan["plan_sha256"]
            or type(max_symbols) is not int or not 1 <= max_symbols <= 10000):
        raise ValueError("Require completed acquisition and bounded symbol batch")
    requests = {r["request_id"]: r for r in plan["requests"]}
    if set(requests) != set(acquisition["completed"]):
        raise ValueError("Incomplete acquisition receipts")
    sources, intervals = defaultdict(dict), defaultdict(list)
    for identity, result in acquisition["completed"].items():
        row = requests[identity]
        symbol = row["symbol"]
        intervals[symbol].append((row["start_ts"], row["end_ts"]))
        if result["status"] == "cached":
            sources[symbol][result["file"]] = {"file": result["file"], "archive_sha256": result["archive_sha256"]}
        elif result["status"] != "no_data":
            raise ValueError("Invalid acquisition receipt")
    for row in plan["reusable_archives"]:
        symbol = row["symbol"]
        sources[symbol][row["file"]] = {"file": row["file"], "archive_sha256": row["archive_sha256"]}
        intervals[symbol].append((row["start_ts"], row["end_ts"]))
    low = int(_time(schedule["start"] + "T00:00:00Z").timestamp())
    high = int(_time(schedule["end"] + "T00:00:00Z").timestamp()) + 86400
    if any(a < low or b > high for windows in intervals.values() for a, b in windows):
        raise ValueError("Calendar does not cover acquisition windows")
    evidence = {}
    for path in sorted(Path(evidence_dir).glob("*_sip_trades.json")):
        artifact = json.loads(path.read_text())
        _verify(artifact, "alpaca-trade-gap-v1", "archive_sha256")
        evidence[path.name] = artifact
    config = {"plan_sha256": plan["plan_sha256"], "calendar_sha256": schedule["calendar_sha256"],
              "acquisition_sha256": hashlib.sha256(encode_dataset(acquisition).encode()).hexdigest(),
              "evidence_snapshot": {k: v["archive_sha256"] for k, v in evidence.items()}}
    output, cache = Path(output), Path(cache)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".audit.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config_path = output / "config.json"
        if config_path.exists() and json.loads(config_path.read_text()) != config:
            raise ValueError("Frozen audit inputs changed; use a new output directory")
        save(config_path, config)
        progress = {"status": "running", "total_symbols": len(intervals), "completed_symbols": 0,
                    "missing_price_minutes": 0, "verified_volume_minutes": 0, "unsupported_condition_minutes": 0,
                    "uncached_minutes": 0, "new_symbols_this_run": 0, "training_started": False, "network_calls": 0}
        for symbol in sorted(intervals, key=lambda s: (-max(b for _, b in intervals[s]), s)):
            if not symbol or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in symbol):
                raise ValueError("Invalid symbol filename")
            path = output / f"{symbol}.json"
            if not path.exists() and progress["new_symbols_this_run"] >= max_symbols:
                break
            progress.update(current_symbol=symbol, updated_at=datetime.now(timezone.utc).isoformat())
            save(output / "progress.json", progress)
            try:
                if path.exists():
                    report = json.loads(path.read_text())
                    _verify(report, "historical-symbol-gap-audit-v1", "audit_sha256")
                    if report["symbol"] != symbol:
                        raise ValueError("Audit symbol mismatch")
                else:
                    report = audit_symbol(symbol, list(sources[symbol].values()), intervals[symbol], schedule, cache, evidence)
                    save(path, report)
                    progress["new_symbols_this_run"] += 1
                progress["completed_symbols"] += 1
                progress["missing_price_minutes"] += report["missing_price_minutes"]
                for key in ("verified_volume_minutes", "unsupported_condition_minutes", "uncached_minutes"):
                    progress[key] += len(report[key])
            except Exception as exc:
                progress.update(status="failed", error_type=type(exc).__name__)
                save(output / "progress.json", progress)
                raise
        progress.update(status="complete" if progress["completed_symbols"] == len(intervals) else "paused_at_batch_limit",
                        updated_at=datetime.now(timezone.utc).isoformat())
        save(output / "progress.json", progress)
        return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cal = sub.add_parser("calendar")
    cal.add_argument("--start", required=True)
    cal.add_argument("--end", required=True)
    cal.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("audit")
    for name in ("plan", "acquisition", "calendar", "cache", "evidence-dir", "output"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument("--max-symbols", type=int, default=20)
    args = parser.parse_args()
    if args.command == "calendar":
        result = calendar(args.start, args.end)
        with args.output.open("x") as stream:
            stream.write(encode_dataset(result) + "\n")
    else:
        print(encode_dataset(audit(json.loads(args.plan.read_text()), json.loads(args.acquisition.read_text()),
            json.loads(args.calendar.read_text()), args.cache, args.evidence_dir, args.output, max_symbols=args.max_symbols)))


if __name__ == "__main__":
    main()
