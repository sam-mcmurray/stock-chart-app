"""Immutable references to deduplicated baseline and repaired session artifacts."""
import argparse
from collections import Counter
from contextlib import ExitStack
import fcntl
import hashlib
import json
from pathlib import Path

from drl_agent.candidate_session_report import report
from drl_agent.candidate_sessions import read, validate_float_evidence, validate_history_boundary, validate_history_volume_scope
from drl_agent.corpus_gap_audit import signed, save
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify
from drl_agent.historical_watch import build_watch
from drl_agent.watch_replay import WatchConfig


def digest(value):
    return hashlib.sha256(encode_dataset(value).encode()).hexdigest()


def contained(root, name):
    path = (root / name).resolve()
    if Path(name).is_absolute() or not path.is_relative_to(root):
        raise ValueError("Dataset path escapes root")
    return path


def build_index(root, plan, repairs=(), *, allow_float_additions=False):
    if type(allow_float_additions) is not bool:
        raise ValueError("Float additions option must be boolean")
    root = Path(root).resolve()
    _verify(plan, "candidate-gap-plan-v1", "plan_sha256")
    planned = {f"{row['symbol']}:{s['date']}" for row in plan["candidates"] for s in row["sessions"]}
    paths = [root] + [contained(root, name) for name in repairs]
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate dataset source")
    selected, sources, previous_config, replacements = {}, [], None, 0
    with ExitStack() as locks:
        for path in sorted(paths):
            lock = locks.enter_context((path / ".assemble.lock").open("r"))
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        for path in paths:
            config, progress = read(path, "config.json"), read(path, "progress.json")
            if config["plan_sha256"] != plan["plan_sha256"] or progress["status"] not in {
                    "complete", "paused_at_batch_limit"}:
                raise ValueError("Unstable or unrelated dataset source")
            policy = {k: v for k, v in config.items() if k not in {
                "trade_hashes", "symbol_scope", "session_scope", "history_extensions", "history_volume_evidence"}}
            allowed_symbols = {r["symbol"] for r in plan["candidates"]}
            if config.get("symbol_scope") is not None:
                allowed_symbols &= set(config["symbol_scope"])
            if config.get("session_scope") is not None:
                allowed_symbols &= {s.split(":")[0] for s in config["session_scope"]}
            float_evidence = validate_float_evidence(config["float_evidence"], allowed_symbols)
            if allow_float_additions:
                policy.pop("float_evidence")
            history_sources = config.get("history_extensions", [])
            # History retention is checked per replaced session below, not across unrelated scopes.
            if previous_config is not None and (policy != previous_config[0]
                    or not previous_config[1] <= set(config["trade_hashes"])):
                raise ValueError("Repair policy changed or evidence removed")
            previous_config = policy, set(config["trade_hashes"])
            readiness = report(path)
            source = path.relative_to(root).as_posix()
            sources.append({"directory": source, "config_sha256": digest(config),
                            "progress_sha256": digest(progress), "sessions": len(progress["completed"])})
            for identity, receipt in progress["completed"].items():
                if identity not in planned:
                    raise ValueError("Session outside parent plan")
                symbol, day = identity.split(":")
                if config.get("symbol_scope") is not None and symbol not in config["symbol_scope"]:
                    raise ValueError("Session outside source scope")
                if config.get("session_scope") is not None and identity not in config["session_scope"]:
                    raise ValueError("Session outside source scope")
                directory = contained(root, f"{source}/{symbol}_{day}")
                artifacts = {}
                for name, version, key in (("joined", "historical-news-price-join-v1", "join_sha256"),
                        ("features", "historical-bar-features-v2", "features_sha256"),
                        ("watch", "historical-watch-v2", "watch_sha256"),
                        ("summary", "candidate-session-summary-v1", "summary_sha256")):
                    artifact = read(directory, name + ".json")
                    _verify(artifact, version, key)
                    if artifact[key] != receipt[key]:
                        raise ValueError("Dataset artifact changed")
                    artifacts[name] = artifact
                features, watch, summary = (artifacts[k] for k in ("features", "watch", "summary"))
                if (features["source_join_sha256"] != receipt["join_sha256"]
                        or watch["source_hashes"] != {symbol: receipt["join_sha256"]}
                        or watch["feature_hashes"] != {symbol: receipt["features_sha256"]}
                        or features["coverage"] != summary["coverage"]):
                    raise ValueError("Dataset artifact lineage mismatch")
                fields = ("candle_id", "decision_ts", "historical_nominal_price", "bar_vwap_estimate",
                          "minute_macd", "minute_macd_signal", "minute_macd_histogram")
                signature = digest({"join": receipt["join_sha256"],
                    "technicals": [[s[k] for k in fields] for s in features["states"]],
                    "policy": {k: watch[k] for k in ("config", "entry_policy", "research_policy", "watch_policy")}})
                joined = artifacts["joined"]
                symbol_float = [row for row in float_evidence if row["symbol"] == symbol]
                float_hashes = {digest(row) for row in symbol_float}
                if (watch["evidence_sha256"] != digest(symbol_float)
                        or summary["float_evidence_supplied"] != bool(symbol_float)
                        or (symbol_float and summary.get("float_evidence_sha256") != digest(symbol_float))):
                    raise ValueError("Float evidence lineage mismatch")
                if symbol_float or watch["evidence"]:
                    expected = build_watch([joined], [features], symbol_float,
                                           config=WatchConfig(**config["watch_config"]))
                    if expected["watch_sha256"] != watch["watch_sha256"]:
                        raise ValueError("Watch does not match declared float evidence and strict rules")
                content_signature = digest({"session": {k: v for k, v in joined.items() if k not in {
                    "join_sha256", "price_export_sha256", "source_file_sha256", "source_archives"}},
                    "technicals": [[s[k] for k in fields] for s in features["states"]],
                    "policy": {k: watch[k] for k in ("config", "entry_policy", "research_policy", "watch_policy")}})
                archive_hashes = {r["archive_sha256"] for r in joined["source_archives"]}
                extensions = [r for r in history_sources if r["symbol"] == symbol]
                history_volume = [r for r in config.get("history_volume_evidence", []) if r["symbol"] == symbol]
                history_volume_hashes = {digest(r) for r in history_volume}
                if history_volume:
                    validate_history_boundary(summary["original_history_start_ts"], extensions)
                    validate_history_volume_scope(summary["original_history_start_ts"], extensions, history_volume)
                if any({k: v for k, v in r.items() if k != "symbol"} not in features["volume_gap_evidence"]
                       for r in history_volume):
                    raise ValueError("History volume evidence lineage mismatch")
                declared = {r["archive_sha256"] for r in extensions}
                if extensions and (sorted(declared) != summary.get("history_extension_hashes")
                        or any(r["end_ts"] >= summary["original_history_start_ts"] for r in extensions)
                        or archive_hashes != set(summary["original_archive_hashes"]) | declared):
                    raise ValueError("Invalid declared history extension")
                counts = {k: summary[k] for k in ("watched_states", "momentum_states", "entry_ready_states")}
                counts.update(sessions=1, rvol_states=summary["coverage"]["rvol_states"])
                old = selected.get(identity)
                if old is not None:
                    if not set(old.get("history_volume_hashes", [])) <= history_volume_hashes:
                        raise ValueError("History volume evidence removed")
                    old_float = set(old.get("float_evidence_hashes", []))
                    if not old_float <= float_hashes or (old_float != float_hashes and not allow_float_additions):
                        raise ValueError("Float evidence removed or additions not enabled")
                    history_only = (bool(extensions) and content_signature == old["session_content_sha256"]
                        and set(old["price_archive_hashes"]) <= archive_hashes
                        and archive_hashes - set(old["price_archive_hashes"]) <= declared)
                    if (signature != old["comparison_sha256"] and not history_only
                            or counts["rvol_states"] < old["counts"]["rvol_states"]):
                        raise ValueError("Repair changes prices/rules or reduces RVOL coverage")
                    replacements += 1
                selected[identity] = {"symbol": symbol, "date": day,
                    "directory": directory.relative_to(root).as_posix(),
                    **{k: receipt[k] for k in ("join_sha256", "features_sha256", "watch_sha256", "summary_sha256")},
                    "comparison_sha256": signature, "session_content_sha256": content_signature,
                    **({"float_evidence_hashes": sorted(float_hashes)} if float_hashes else {}),
                    **({"history_volume_hashes": sorted(history_volume_hashes)} if history_volume_hashes else {}),
                    "price_archive_hashes": sorted(archive_hashes), "counts": counts}
            if readiness["totals"].get("sessions", 0) != len(progress["completed"]):
                raise ValueError("Source readiness count mismatch")
    totals = Counter()
    for row in selected.values():
        totals.update(row["counts"])
    return signed({"version": "candidate-dataset-index-v1", "plan_sha256": plan["plan_sha256"],
        "sources": sources, "selection_policy": "explicit_repair_order_with_evidence_superset_and_unchanged_prices_rules",
        "sessions": dict(sorted(selected.items())), "totals": dict(totals), "replacements": replacements,
        "planned_sessions": len(planned), "missing_sessions": sorted(planned - selected.keys()),
        "training_ready": False, "training_started": False, "network_calls": 0, "broker_calls": 0,
        **({"float_revision_policy": "explicit_additions_only_recomputed_strict_watch"} if allow_float_additions else {}),
        "limitations": ["References are relative to the baseline root; no artifacts copied or overwritten",
            "Consumers must verify index and artifact hashes before reading",
            "Coverage is not profitability; float and execution gates remain",
            "No train/evaluation split or portfolio replay has been certified"]}, "index_sha256")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--repair", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-float-additions", action="store_true",
                        help="Permit added dated float evidence, never removal or changed entry rules")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = build_index(args.root, json.loads(args.plan.read_text()), args.repair,
                         allow_float_additions=args.allow_float_additions)
    save(args.output, result)
    print(encode_dataset({"totals": result["totals"], "replacements": result["replacements"],
                         "missing_sessions": len(result["missing_sessions"]), "index_sha256": result["index_sha256"]}))


if __name__ == "__main__":
    main()
