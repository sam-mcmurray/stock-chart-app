"""Prioritize existing gap evidence by causal news/price watch admission, not returns."""
import argparse
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time

from dotenv import dotenv_values

from drl_agent.alpaca_data_cache import MarketDataHTTPError
from drl_agent.alpaca_replay_export import build_export, merge_exports
from drl_agent.alpaca_trade_gap import cache_minute
from drl_agent.corpus_gap_audit import save, signed
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify
from drl_agent.trade_volume_evidence import VOLUME_POLICY, volume_minutes
from drl_agent.watch_replay import WatchConfig


def candidates(candles, articles, sessions, config):
    news = sorted(set(a["replay_available_ts"] for a in articles))
    openings = [s["open_ts"] for s in sessions]
    counts = defaultdict(int)
    for candle in candles:
        now = candle["end_ts"]
        index = bisect_right(openings, candle["start_ts"]) - 1
        if index < 0 or now > sessions[index]["close_ts"]:
            continue
        cursor = bisect_right(news, now) - 1
        if (cursor >= 0 and now < news[cursor] + config.watch_window_seconds
                and config.minimum_price <= candle["close"] <= config.maximum_price):
            counts[index] += 1
    return [{**sessions[i], "watch_states": count} for i, count in sorted(counts.items())]


def build_plan(corpus, audit_dir, cache, schedule, config, *, notify=None):
    _verify(schedule, "historical-gap-calendar-v1", "calendar_sha256")
    progress = json.loads((audit_dir / "progress.json").read_text())
    if progress["status"] != "complete":
        raise ValueError("Require completed gap audit")
    audit_config = json.loads((audit_dir / "config.json").read_text())
    if audit_config["calendar_sha256"] != schedule["calendar_sha256"]:
        raise ValueError("Gap audit calendar changed")
    corpus_progress = json.loads((corpus / "progress.json").read_text())
    if corpus_progress["status"] != "complete":
        raise ValueError("Require completed news corpus")
    by_symbol, shard_hashes = defaultdict(list), []
    for row in corpus_progress["completed"]:
        if Path(row["file"]).name != row["file"]:
            raise ValueError("Invalid corpus filename")
        shard = json.loads((corpus / row["file"]).read_text())
        _verify(shard, "retrospective-news-universe-v1", "manifest_sha256")
        if shard["manifest_sha256"] != row["manifest_sha256"]:
            raise ValueError("Changed corpus shard")
        shard_hashes.append(shard["manifest_sha256"])
        for article in shard["candidates"]:
            for symbol in article["tickers"]:
                by_symbol[symbol].append(article)
    result = {"version": "candidate-gap-plan-v1", "source_news_shards": shard_hashes,
              "calendar_sha256": schedule["calendar_sha256"], "symbols_audited": 0,
              "source_gap_audit_config": audit_config,
              "minimum_price": config.minimum_price, "maximum_price": config.maximum_price,
              "watch_window_seconds": config.watch_window_seconds, "candidates": [], "excluded": {},
              "network_calls": 0, "training_started": False,
              "limitations": ["Data acquisition priority is not portfolio watch-capacity selection or entry eligibility",
                  "RVOL, float, technicals and execution gates remain required",
                  "All price/news-qualified dates retained regardless of subsequent profit",
                  "Only fully requested prior-session gaps are included; unrequested history still needs audit"]}
    reports = sorted(audit_dir.glob("*.json"))
    for path in reports:
        if path.name in {"config.json", "progress.json"}:
            continue
        report = json.loads(path.read_text())
        _verify(report, "historical-symbol-gap-audit-v1", "audit_sha256")
        symbol = report["symbol"]
        exports = []
        for source in report["source_archives"]:
            if Path(source["file"]).name != source["file"]:
                raise ValueError("Invalid price filename")
            archive = json.loads((cache / source["file"]).read_text())
            if archive["archive_sha256"] != source["archive_sha256"]:
                raise ValueError("Changed price source")
            exported = build_export(archive)
            if exported["symbol"] != symbol:
                raise ValueError("Wrong price symbol")
            exports.append(exported)
        candles = merge_exports(exports)["candles"] if exports else []
        selected = candidates(candles, by_symbol[symbol], schedule["sessions"], config)
        result["symbols_audited"] += 1
        if notify and result["symbols_audited"] % 25 == 0:
            notify({"symbols_audited": result["symbols_audited"], "total_symbols": progress["total_symbols"],
                    "candidate_symbols": len(result["candidates"]), "current_symbol": symbol})
        if not selected:
            result["excluded"][symbol] = "no_observed_news_price_watch_state" if candles else "no_price_bars"
            continue
        prior = {s["open_ts"]: s for day in selected for s in
                 [s for s in schedule["sessions"] if s["close_ts"] <= day["open_ts"]][-90:]}
        starts = sorted(prior)
        def needed(stamp):
            index = bisect_right(starts, stamp) - 1
            return index >= 0 and stamp < prior[starts[index]]["close_ts"]
        result["candidates"].append({"symbol": symbol, "source_audit_sha256": report["audit_sha256"],
            "sessions": selected, "uncached_prior_minutes": [t for t in report["uncached_minutes"] if needed(t)],
            "unsupported_prior_minutes": [t for t in report["unsupported_condition_minutes"] if needed(t)],
            "verified_prior_minutes": [t for t in report["verified_volume_minutes"] if needed(t)]})
    if result["symbols_audited"] != progress["total_symbols"]:
        raise ValueError("Missing symbol audit reports")
    result["candidates"].sort(key=lambda r: (-max(s["close_ts"] for s in r["sessions"]),
                                             len(r["uncached_prior_minutes"]), r["symbol"]))
    return signed(result, "plan_sha256")


def repair_plan(plan, readiness, receipts, cache, *, max_minutes=100, max_symbol_minutes=None):
    _verify(plan, "candidate-gap-plan-v1", "plan_sha256")
    _verify(readiness, "candidate-session-readiness-v1", "report_sha256")
    if (readiness["plan_sha256"] != plan["plan_sha256"] or receipts["plan_sha256"] != plan["plan_sha256"]):
        raise ValueError("Repair sources use different plans")
    if type(max_minutes) is not int or not 1 <= max_minutes <= 10000:
        raise ValueError("Require bounded repair plan")
    if max_symbol_minutes is not None and (type(max_symbol_minutes) is not int or not 1 <= max_symbol_minutes <= 10000):
        raise ValueError("Invalid per-symbol repair limit")
    cache = Path(cache)
    cached_names = {p.name for p in cache.glob("*_sip_trades.json")}
    verified, hashes = set(), []
    for identity, receipt in receipts["completed"].items():
        if Path(receipt["file"]).name != receipt["file"]:
            raise ValueError("Invalid evidence filename")
        artifact = json.loads((cache / receipt["file"]).read_text())
        _verify(artifact, "alpaca-trade-gap-v1", "archive_sha256")
        if artifact["archive_sha256"] != receipt["archive_sha256"]:
            raise ValueError("Verified cache changed")
        symbol, stamp = identity.split(":")
        if classify(artifact, symbol, int(stamp))["status"] == "volume_verified":
            verified.add((symbol, int(stamp)))
        hashes.append(artifact["archive_sha256"])
    leads = defaultdict(set)
    for row in readiness["volume_repair_sessions"]:
        leads[row["symbol"]].add(row["date"])
    selected, skipped_large = [], {}
    for row in plan["candidates"]:
        sessions = [s for s in row["sessions"] if s["date"] in leads[row["symbol"]]]
        if not sessions:
            continue
        latest = max(s["open_ts"] for s in sessions)
        minutes = []
        for stamp in sorted(set(row["uncached_prior_minutes"] + row.get("unsupported_prior_minutes", [])), reverse=True):
            if stamp >= latest or (row["symbol"], stamp) in verified:
                continue
            name = f"{row['symbol']}_{datetime.fromtimestamp(stamp, timezone.utc):%Y%m%dT%H%MZ}_sip_trades.json"
            if name in cached_names:
                artifact = json.loads((cache / name).read_text())
                status = classify(artifact, row["symbol"], stamp)["status"]
                hashes.append(artifact["archive_sha256"])
                if status == "volume_verified":
                    verified.add((row["symbol"], stamp))
                    continue
            minutes.append(stamp)
        if max_symbol_minutes is not None and len(minutes) > max_symbol_minutes:
            skipped_large[row["symbol"]] = len(minutes)
            continue
        if minutes:
            selected.append({**row, "sessions": sessions, "uncached_prior_minutes": minutes,
                             "unsupported_prior_minutes": [], "verified_prior_minutes": []})
    selected.sort(key=lambda r: (-max(s["open_ts"] for s in r["sessions"]),
                                len(r["uncached_prior_minutes"]), r["symbol"]))
    bounded, remaining = [], max_minutes
    for row in selected:
        if not remaining:
            break
        minutes = row["uncached_prior_minutes"][:remaining]
        bounded.append({**row, "uncached_prior_minutes": minutes})
        remaining -= len(minutes)
    return signed({**{k: v for k, v in plan.items() if k not in {"plan_sha256", "candidates"}},
        "candidates": bounded, "source_plan_sha256": plan["plan_sha256"],
        "source_readiness_sha256": readiness["report_sha256"], "source_trade_hashes": sorted(set(hashes)),
        "repair_policy": "technical_pass_unknown_rvol_newest_session_then_fewest_unverified_minutes",
        "volume_policy": VOLUME_POLICY, "maximum_minutes": max_minutes, "network_calls": 0,
        "maximum_symbol_minutes": max_symbol_minutes, "skipped_larger_repairs": skipped_large,
        "training_started": False}, "plan_sha256")


def classify(artifact, symbol, stamp):
    # Validate identity even when the trade conditions themselves are unsupported.
    from drl_agent.alpaca_trade_gap import stamp_ns
    if stamp_ns(artifact["request"]["start"]) != stamp * 10**9:
        raise ValueError("Trade evidence minute changed")
    try:
        derived = volume_minutes([artifact], symbol)
        return {"status": "volume_verified", "volume": derived[stamp]["volume"], "policy": VOLUME_POLICY}
    except ValueError as exc:
        if str(exc) != "Unsupported volume evidence trade conditions":
            raise
        return {"status": "unsupported_conditions", "volume": None, "policy": VOLUME_POLICY}


def verify(plan, cache, output, *, max_requests=100, fetch=cache_minute, pause=time.sleep, notify=None,
           cached_only=False):
    _verify(plan, "candidate-gap-plan-v1", "plan_sha256")
    if type(max_requests) is not int or not 1 <= max_requests <= 10000:
        raise ValueError("Require bounded verification batch")
    cache, output = Path(cache), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cached_names = {p.name for p in cache.glob("*_sip_trades.json")} if cached_only else set()
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    with (output / ".verify.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = output / "progress.json"
        progress = json.loads(path.read_text()) if path.exists() else {"plan_sha256": plan["plan_sha256"], "completed": {}}
        if progress["plan_sha256"] != plan["plan_sha256"]:
            raise ValueError("Verification plan changed")
        attempted = 0
        tasks = [(r["symbol"], t) for r in plan["candidates"] for t in sorted(r["uncached_prior_minutes"], reverse=True)]
        identities = {f"{symbol}:{stamp}" for symbol, stamp in tasks}
        if not progress["completed"].keys() <= identities:
            raise ValueError("Completed evidence is outside verification plan")
        for identity, saved in progress["completed"].items():
            if Path(saved["file"]).name != saved["file"]:
                raise ValueError("Invalid evidence filename")
            artifact = json.loads((cache / saved["file"]).read_text())
            _verify(artifact, "alpaca-trade-gap-v1", "archive_sha256")
            if artifact["archive_sha256"] != saved["archive_sha256"]:
                raise ValueError("Verified cache changed")
            symbol, stamp = identity.split(":")
            saved.update(classify(artifact, symbol, int(stamp)))
        progress["volume_policy"] = VOLUME_POLICY
        progress.pop("error_type", None)
        progress.pop("http_status", None)
        progress.update(total_minutes=len(tasks), remaining=len(tasks)-len(progress["completed"]))
        for symbol, stamp in tasks:
            identity = f"{symbol}:{stamp}"
            if identity in progress["completed"]:
                continue
            cached_name = f"{symbol}_{datetime.fromtimestamp(stamp, timezone.utc):%Y%m%dT%H%MZ}_sip_trades.json"
            if cached_only and cached_name not in cached_names:
                continue
            if not cached_only and attempted >= max_requests:
                break
            progress.update(status="running", symbol=symbol, minute_ts=stamp,
                            updated_at=datetime.now(timezone.utc).isoformat())
            if not cached_only or attempted == 0:
                save(path, progress)
            try:
                if cached_only:
                    result = {"file": cache / cached_name}
                else:
                    result = fetch(symbol, datetime.fromtimestamp(stamp, timezone.utc).isoformat(), cache,
                        key=os.getenv("APCA_API_KEY_ID") or env.get("APCA_API_KEY_ID"),
                        secret=os.getenv("APCA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY"))
                artifact = json.loads(Path(result["file"]).read_text())
                classification = classify(artifact, symbol, stamp)
                status = classification["status"]
                progress["completed"][identity] = {"file": Path(result["file"]).name, **classification,
                    "archive_sha256": artifact["archive_sha256"]}
                attempted += 1
                progress["remaining"] = len(tasks) - len(progress["completed"])
                # Offline receipts can be revalidated after interruption without another download.
                if not cached_only or attempted % 100 == 0:
                    save(path, progress)
                if notify:
                    notify({"symbol": symbol, "minute_ts": stamp, "status": status,
                            "completed": len(progress["completed"]), "remaining": progress["remaining"]})
                if not cached_only:
                    pause(1)
            except Exception as exc:
                progress.update(status="failed", error_type=type(exc).__name__)
                if isinstance(exc, MarketDataHTTPError):
                    progress["http_status"] = exc.status_code
                save(path, progress)
                raise RuntimeError("Gap verification stopped; see sanitized progress") from None
        progress.update(status="complete" if len(progress["completed"]) == len(tasks) else
                        ("cache_exhausted" if cached_only else "paused_at_batch_limit"),
                        total_minutes=len(tasks), remaining=len(tasks)-len(progress["completed"]),
                        updated_at=datetime.now(timezone.utc).isoformat())
        save(path, progress)
        return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("plan")
    for name in ("corpus", "audit-dir", "cache", "calendar", "config", "output"):
        build.add_argument("--"+name, type=Path, required=True)
    run = sub.add_parser("verify")
    for name in ("plan", "cache", "output"):
        run.add_argument("--"+name, type=Path, required=True)
    run.add_argument("--max-requests", type=int, default=100)
    run.add_argument("--cached-only", action="store_true",
                     help="Reclassify receipts and discover planned cached minutes without downloads")
    repair = sub.add_parser("repair-plan")
    for name in ("plan", "readiness", "verification", "cache", "output"):
        repair.add_argument("--" + name, type=Path, required=True)
    repair.add_argument("--max-minutes", type=int, default=100)
    repair.add_argument("--max-symbol-minutes", type=int)
    args = parser.parse_args()
    if args.command == "plan":
        if args.output.exists():
            raise FileExistsError(args.output)
        result = build_plan(args.corpus, args.audit_dir, args.cache, json.loads(args.calendar.read_text()),
                            WatchConfig(**json.loads(args.config.read_text())["watch_config"]),
                            notify=lambda row: print(encode_dataset(row), flush=True))
        save(args.output, result)
        print(encode_dataset({"candidate_symbols": len(result["candidates"]), "excluded_symbols": len(result["excluded"]),
                              "uncached_prior_minutes": sum(len(r["uncached_prior_minutes"]) for r in result["candidates"])}))
    elif args.command == "repair-plan":
        if args.output.exists():
            raise FileExistsError(args.output)
        result = repair_plan(json.loads(args.plan.read_text()), json.loads(args.readiness.read_text()),
            json.loads(args.verification.read_text()), args.cache, max_minutes=args.max_minutes,
            max_symbol_minutes=args.max_symbol_minutes)
        save(args.output, result)
        print(encode_dataset({"symbols": [r["symbol"] for r in result["candidates"]],
            "minutes": sum(len(r["uncached_prior_minutes"]) for r in result["candidates"]),
            "plan_sha256": result["plan_sha256"]}))
    else:
        result = verify(json.loads(args.plan.read_text()), args.cache, args.output, max_requests=args.max_requests,
                        cached_only=args.cached_only,
                        notify=lambda row: print(encode_dataset(row), flush=True))
        print(encode_dataset({k: v for k, v in result.items() if k != "completed"}))


if __name__ == "__main__":
    main()
