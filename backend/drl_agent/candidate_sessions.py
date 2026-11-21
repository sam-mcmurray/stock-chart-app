"""Offline, resumable session assembly from a frozen candidate plan and caches."""
import argparse
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from drl_agent.alpaca_data_cache import _time
from drl_agent.alpaca_replay_export import build_export
from drl_agent.corpus_gap_audit import save, signed
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify
from drl_agent.historical_symbol_session import build_session
from drl_agent.historical_watch import build_watch, prepare_evidence
from drl_agent.trade_volume_evidence import VOLUME_POLICY, volume_minutes
from drl_agent.watch_replay import WatchConfig


def read(root, name):
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("Invalid source filename")
    return json.loads((root / name).read_text())


def exposed_history_boundary(original_start, extensions):
    if not extensions:
        return
    day = datetime.fromtimestamp(original_start, ZoneInfo("America/New_York")).date()
    calendar = xcals.get_calendar("XNYS", start=day - timedelta(days=7), end=day + timedelta(days=7))
    sessions = calendar.sessions_in_range(str(day), str(day))
    if not len(sessions):
        return
    opening = int(calendar.session_open(sessions[0]).timestamp())
    closing = int(calendar.session_close(sessions[0]).timestamp())
    if original_start <= opening or min(r["start_ts"] for r in extensions) > opening:
        return
    return opening, closing


def validate_history_boundary(original_start, extensions):
    boundary = exposed_history_boundary(original_start, extensions)
    if boundary is None:
        return
    opening, closing = boundary
    # Earlier history makes the formerly excluded partial boundary day part of the baseline.
    missing = [stamp for stamp in range(opening, min(original_start, closing), 60)
               if not any(r["start_ts"] <= stamp and stamp + 59 <= r["end_ts"] for r in extensions)]
    if missing:
        raise ValueError(f"History extension exposes {len(missing)} unrequested boundary minutes; extend raw coverage")


def validate_history_volume_scope(original_start, extensions, rows):
    if not rows:
        return
    boundary = exposed_history_boundary(original_start, extensions)
    for row in rows:
        inside_extension = any(r["start_ts"] <= row["start_ts"] and row["available_ts"] - 1 <= r["end_ts"]
                               for r in extensions)
        inside_boundary = boundary is not None and boundary[0] <= row["start_ts"] and row["available_ts"] <= boundary[1]
        if not (inside_extension or inside_boundary):
            raise ValueError("Trade evidence outside history extension or exposed boundary session")


def validate_float_evidence(evidence, symbols):
    prepare_evidence(evidence, symbols)
    if any(row["kind"] != "float" or "evidence_id" in row for row in evidence):
        raise ValueError("Require raw float-only evidence")
    return sorted((dict(row) for row in evidence), key=encode_dataset)


def load_float_evidence(path):
    value = json.loads(path.read_text())
    if isinstance(value, dict):
        _verify(value, "sec-float-evidence-v1", "evidence_sha256")
        value = value["evidence"]
    if not isinstance(value, list):
        raise ValueError("Require a float evidence array or signed SEC evidence artifact")
    return value


@contextmanager
def record_failure(path, state):
    try:
        yield
    except Exception as exc:
        state.update(status="failed", error_type=type(exc).__name__,
                     updated_at=datetime.now(timezone.utc).isoformat())
        save(path, state)
        raise


def session_coverage(session, archives, joined):
    expected = set(range(session["open_ts"], session["close_ts"], 60))
    windows = [(int(_time(a["request"]["start"]).timestamp()), int(_time(a["request"]["end"]).timestamp()))
               for a in archives]
    # Historical bar queries bound the bar's start timestamp, including the end.
    requested = {t for t in expected if any(start <= t <= end for start, end in windows)}
    observed = {c["start_ts"] for c in joined["candles"]} & expected
    return {"expected_regular_minutes": len(expected), "requested_regular_minutes": len(requested),
            "observed_regular_minutes": len(observed), "unrequested_regular_minutes": len(expected - requested),
            "requested_without_price_bar": len(requested - observed),
            "full_regular_price_session": expected == observed}


def assemble(plan, corpus, audit_dir, prices, trades, verification, output, config, *, max_sessions=7, notify=None,
             symbols=None, history_archives=(), sessions=None, float_evidence=(), history_trades=()):
    _verify(plan, "candidate-gap-plan-v1", "plan_sha256")
    if type(max_sessions) is not int or not 1 <= max_sessions <= 1000:
        raise ValueError("Require bounded session batch")
    if symbols is not None:
        if (not isinstance(symbols, (list, tuple)) or not symbols
                or any(not isinstance(s, str) for s in symbols)
                or len(set(symbols)) != len(symbols)
                or not set(symbols) <= {row["symbol"] for row in plan["candidates"]}):
            raise ValueError("Invalid assembly symbol scope")
        symbols = sorted(symbols)
    if sessions is not None:
        planned = {f"{r['symbol']}:{s['date']}" for r in plan["candidates"] for s in r["sessions"]
                   if symbols is None or r["symbol"] in symbols}
        if (not isinstance(sessions, (list, tuple)) or not sessions
                or any(not isinstance(s, str) for s in sessions)
                or len(set(sessions)) != len(sessions) or not set(sessions) <= planned):
            raise ValueError("Invalid assembly session scope")
        sessions = sorted(sessions)
    selected_sessions = set(sessions) if sessions is not None else None
    evidence_symbols = {r["symbol"] for r in plan["candidates"] if symbols is None or r["symbol"] in symbols}
    if sessions is not None:
        evidence_symbols &= {identity.split(":")[0] for identity in sessions}
    float_evidence = validate_float_evidence(float_evidence, evidence_symbols)
    history, history_sources = {}, []
    allowed = set(symbols) if symbols is not None else {r["symbol"] for r in plan["candidates"]}
    for archive in history_archives:
        exported = build_export(archive)
        symbol = exported["symbol"]
        if symbol not in allowed or archive["archive_sha256"] in {r["archive_sha256"] for r in history_sources}:
            raise ValueError("Invalid history extension scope or duplicate archive")
        history.setdefault(symbol, []).append(archive)
        history_sources.append({"symbol": symbol, "archive_sha256": archive["archive_sha256"],
            "start_ts": int(_time(archive["request"]["start"]).timestamp()),
            "end_ts": int(_time(archive["request"]["end"]).timestamp())})
    if (plan["minimum_price"], plan["maximum_price"], plan["watch_window_seconds"]) != (
            config.minimum_price, config.maximum_price, config.watch_window_seconds):
        raise ValueError("Watch configuration differs from candidate plan")
    if verification["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("Verification plan changed")
    progress = read(corpus, "progress.json")
    if progress["status"] != "complete":
        raise ValueError("Require completed news corpus")
    shards = []
    for record in progress["completed"]:
        shard = read(corpus, record["file"])
        _verify(shard, "retrospective-news-universe-v1", "manifest_sha256")
        if shard["manifest_sha256"] != record["manifest_sha256"]:
            raise ValueError("Changed news shard")
        shards.append(shard)
    if [s["manifest_sha256"] for s in shards] != plan["source_news_shards"]:
        raise ValueError("News corpus differs from candidate plan")
    evidence, evidence_hashes = {}, []
    for identity, receipt in sorted(verification["completed"].items()):
        artifact = read(trades, receipt["file"])
        _verify(artifact, "alpaca-trade-gap-v1", "archive_sha256")
        if artifact["archive_sha256"] != receipt["archive_sha256"]:
            raise ValueError("Changed trade evidence")
        symbol, stamp = identity.split(":")
        try:
            derived = volume_minutes([artifact], symbol)
        except ValueError as exc:
            if str(exc) != "Unsupported volume evidence trade conditions":
                raise
            continue
        if set(derived) != {int(stamp)}:
            raise ValueError("Trade minute mismatch")
        evidence.setdefault(symbol, []).append(artifact)
        evidence_hashes.append(artifact["archive_sha256"])
    history_volume = []
    for artifact in history_trades:
        symbol = artifact["url"].split("/")[-2]
        derived = volume_minutes([artifact], symbol)
        stamp = next(iter(derived))
        if symbol not in history:
            raise ValueError("Trade evidence outside history extension")
        if stamp in volume_minutes(evidence.get(symbol, []), symbol):
            raise ValueError("Duplicate history trade minute")
        evidence.setdefault(symbol, []).append(artifact)
        history_volume.append({"symbol": symbol, **derived[stamp]})
    # Prioritize usable cached coverage, never subsequent profit or RVOL outcomes.
    tasks = []
    for row in plan["candidates"]:
        if symbols is not None and row["symbol"] not in symbols:
            continue
        verified = set(volume_minutes(evidence.get(row["symbol"], []), row["symbol"]))
        missing = len((set(row["uncached_prior_minutes"]) | set(row["unsupported_prior_minutes"])
                       | set(row["verified_prior_minutes"])) - verified)
        for session in row["sessions"]:
            if selected_sessions is not None and f"{row['symbol']}:{session['date']}" not in selected_sessions:
                continue
            tasks.append((session["date"], missing, row["symbol"], row, session))
    tasks.sort(key=lambda t: (-int(t[0].replace("-", "")), t[1], t[2]))
    settings = {"version": "candidate-session-batch-v3", "plan_sha256": plan["plan_sha256"],
                "trade_hashes": sorted(evidence_hashes), "volume_policy": VOLUME_POLICY,
                "watch_config": asdict(config), "float_evidence": float_evidence, "order": "newest_date_then_unresolved_gaps_then_symbol"}
    if symbols is not None:
        settings["symbol_scope"] = symbols
    if sessions is not None:
        settings["session_scope"] = sessions
    if history_sources:
        settings["history_extensions"] = sorted(history_sources, key=lambda r: r["archive_sha256"])
    if history_volume:
        settings["history_volume_evidence"] = sorted(history_volume, key=lambda r: (r["symbol"], r["start_ts"]))
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".assemble.lock").open("a") as lock, ExitStack() as checkpoints:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (output / "config.json").exists():
            if read(output, "config.json") != settings:
                raise ValueError("Frozen assembly inputs changed; use a new output directory")
        else:
            save(output / "config.json", settings)
        state = read(output, "progress.json") if (output / "progress.json").exists() else {"completed": {}}
        checkpoints.enter_context(record_failure(output / "progress.json", state))
        state.pop("error_type", None)
        processed = 0
        loaded_symbol, archives = None, None
        for day, missing, symbol, row, session in tasks:
            identity = f"{symbol}:{day}"
            directory = output / f"{symbol}_{day}"
            if identity in state["completed"]:
                for name, version, key in (("joined", "historical-news-price-join-v1", "join_sha256"),
                    ("features", "historical-bar-features-v2", "features_sha256"),
                    ("watch", "historical-watch-v2", "watch_sha256"),
                    ("summary", "candidate-session-summary-v1", "summary_sha256")):
                    artifact = read(directory, name + ".json")
                    _verify(artifact, version, key)
                    if artifact[key] != state["completed"][identity][key]:
                        raise ValueError("Completed session changed")
                continue
            if processed >= max_sessions:
                break
            state.update(status="running", current=identity, total_sessions=len(tasks),
                         remaining=len(tasks) - len(state["completed"]),
                         updated_at=datetime.now(timezone.utc).isoformat())
            save(output / "progress.json", state)
            report = read(audit_dir, symbol + ".json")
            _verify(report, "historical-symbol-gap-audit-v1", "audit_sha256")
            if report["symbol"] != symbol or report["audit_sha256"] != row["source_audit_sha256"]:
                raise ValueError("Candidate source audit changed")
            if loaded_symbol != symbol:
                archives = [read(prices, source["file"]) for source in report["source_archives"]]
                if any(a["archive_sha256"] != s["archive_sha256"] for a, s in zip(archives, report["source_archives"])):
                    raise ValueError("Price archive changed")
                loaded_symbol = symbol
            original_start = min(int(_time(a["request"]["start"]).timestamp()) for a in archives)
            extensions = [r for r in history_sources if r["symbol"] == symbol]
            if any(r["end_ts"] >= original_start for r in extensions):
                raise ValueError("History extension must end before original requested history")
            validate_history_boundary(original_start, extensions)
            validate_history_volume_scope(original_start, extensions,
                                          [r for r in history_volume if r["symbol"] == symbol])
            joined, features = build_session(archives + history.get(symbol, []), shards, day,
                                              trade_evidence=evidence.get(symbol, []))
            if joined["symbol"] != symbol:
                raise ValueError("Wrong price symbol")
            symbol_float = [row for row in float_evidence if row["symbol"] == symbol]
            watch = build_watch([joined], [features], symbol_float, config=config)
            candidates = [c for frame in watch["frames"] for c in frame["watching"]]
            summary = signed({"version": "candidate-session-summary-v1", "symbol": symbol, "date": day,
                "join_sha256": joined["join_sha256"], "features_sha256": features["features_sha256"],
                "watch_sha256": watch["watch_sha256"], "coverage": features["coverage"],
                "session_coverage": session_coverage(session, archives, joined),
                "news_active_states": joined["news_active_states"], "articles": len(joined["articles"]),
                "watched_states": len(candidates), "momentum_states": sum(c["momentum_triggered"] for c in candidates),
                "entry_ready_states": sum(c["research_entry_ready"] for c in candidates),
                "entry_block_counts": watch["entry_block_counts"], "rejection_counts": watch["rejection_counts"],
                "unresolved_planned_minutes": missing, "float_evidence_supplied": bool(symbol_float),
                **({"float_evidence_sha256": watch["evidence_sha256"]} if symbol_float else {}),
                "training_started": False, "network_calls": 0, "broker_calls": 0,
                **({"history_extension_hashes": sorted(r["archive_sha256"] for r in extensions),
                    "original_history_start_ts": original_start,
                    "original_archive_hashes": sorted(a["archive_sha256"] for a in archives)} if extensions else {})},
                "summary_sha256")
            directory.mkdir(exist_ok=True)
            for name, artifact in (("joined", joined), ("features", features), ("watch", watch), ("summary", summary)):
                save(directory / (name + ".json"), artifact)
            state["completed"][identity] = {key: summary[key] for key in (
                "join_sha256", "features_sha256", "watch_sha256", "summary_sha256")}
            processed += 1
            state.update(remaining=len(tasks) - len(state["completed"]),
                         updated_at=datetime.now(timezone.utc).isoformat())
            save(output / "progress.json", state)
            if notify:
                notify(summary)
        state.update(status="complete" if len(state["completed"]) == len(tasks) else "paused_at_batch_limit",
                     total_sessions=len(tasks), remaining=len(tasks) - len(state["completed"]),
                     updated_at=datetime.now(timezone.utc).isoformat())
        save(output / "progress.json", state)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "corpus", "audit-dir", "prices", "trades", "verification", "output", "config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--max-sessions", type=int, default=7)
    parser.add_argument("--symbol", action="append", help="Explicit repair scope; retains all planned dates per symbol")
    parser.add_argument("--session", action="append", help="Explicit SYMBOL:YYYY-MM-DD subset, frozen on resume")
    parser.add_argument("--float-evidence", type=Path, help="Dated float array or signed SEC evidence artifact; no price overrides")
    parser.add_argument("--history-archive", type=Path, action="append", default=[],
                        help="Verified raw archive strictly earlier than the original requested history")
    parser.add_argument("--history-trade", type=Path, action="append", default=[],
                        help="Raw SIP trade-minute evidence within an extension or its exposed partial boundary session")
    args = parser.parse_args()
    result = assemble(json.loads(args.plan.read_text()), args.corpus, args.audit_dir, args.prices, args.trades,
        json.loads(args.verification.read_text()), args.output,
        WatchConfig(**json.loads(args.config.read_text())["watch_config"]), max_sessions=args.max_sessions,
        symbols=args.symbol,
        sessions=args.session,
        float_evidence=load_float_evidence(args.float_evidence) if args.float_evidence else [],
        history_archives=[json.loads(p.read_text()) for p in args.history_archive],
        history_trades=[json.loads(p.read_text()) for p in args.history_trade],
        notify=lambda row: print(encode_dataset(row), flush=True))
    from drl_agent.candidate_session_report import report
    save(args.output / "readiness.json", report(args.output))
    print(encode_dataset({k: v for k, v in result.items() if k != "completed"}))


if __name__ == "__main__":
    main()
