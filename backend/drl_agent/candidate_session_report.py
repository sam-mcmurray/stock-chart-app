"""Summarize verified session artifacts; data readiness is not model performance."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from drl_agent.candidate_sessions import read
from drl_agent.corpus_gap_audit import save, signed
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def report(root):
    progress = read(root, "progress.json")
    config = read(root, "config.json")
    totals = Counter()
    blockers = Counter()
    rvol_unavailable = Counter()
    repair_sessions = []
    momentum_sessions = []
    symbols, dates, hashes = {}, {}, []
    for identity, receipt in sorted(progress["completed"].items()):
        symbol, day = identity.split(":")
        name = f"{symbol}_{day}"
        if Path(name).name != name:
            raise ValueError("Invalid session identity")
        directory = root / name
        summary = read(directory, "summary.json")
        watch = read(directory, "watch.json")
        for artifact, version, key in ((summary, "candidate-session-summary-v1", "summary_sha256"),
                                      (watch, "historical-watch-v2", "watch_sha256")):
            _verify(artifact, version, key)
            if artifact[key] != receipt[key]:
                raise ValueError("Completed session changed")
        if (summary["symbol"] != symbol or summary["date"] != day
                or summary["watch_sha256"] != watch["watch_sha256"]
                or summary["join_sha256"] != receipt["join_sha256"]
                or summary["features_sha256"] != receipt["features_sha256"]
                or watch["config"] != config["watch_config"]):
            raise ValueError("Session report provenance mismatch")
        candidates = [c for frame in watch["frames"] for c in frame["watching"]]
        float_only = sum(set(c["entry_block_reasons"]) == {"historical_float_unknown"} for c in candidates)
        # Repair priority only: price/news and technical gates pass, but volume
        # is unknown. Do not interpret missing RVOL as a momentum trigger.
        volume_needed = sum(not c["rvol_valid"] and "rvol_unknown" in c["entry_block_reasons"]
            and set(c["entry_block_reasons"]) <= {"rvol_unknown", "historical_float_unknown"} for c in candidates)
        if volume_needed:
            repair_sessions.append({"symbol": symbol, "date": day, "states": volume_needed,
                                    "summary_sha256": summary["summary_sha256"]})
        counts = {"sessions": 1, "watched_states": len(candidates),
            "momentum_states": sum(c["momentum_triggered"] for c in candidates),
            "entry_ready_states": sum(c["research_entry_ready"] for c in candidates),
            "rvol_states": summary["coverage"]["rvol_states"],
            "full_regular_price_sessions": int(summary["session_coverage"]["full_regular_price_session"]),
            "float_only_blocked_states": float_only, "float_only_blocked_sessions": int(float_only > 0),
            "volume_evidence_needed_states": volume_needed,
            "volume_evidence_needed_sessions": int(volume_needed > 0)}
        if any(summary[key] != counts[key] for key in ("watched_states", "momentum_states", "entry_ready_states")):
            raise ValueError("Session summary counts disagree with watch")
        actual_blockers = Counter(reason for c in candidates for reason in c["entry_block_reasons"])
        if dict(actual_blockers) != summary["entry_block_counts"]:
            raise ValueError("Session blocker counts disagree with watch")
        triggered = [c for c in candidates if c["momentum_triggered"]]
        if triggered:
            momentum_sessions.append({"symbol": symbol, "date": day,
                "states": len(triggered),
                "maximum_rvol": max(c["rvol"] for c in triggered),
                "entry_ready_states": sum(c["research_entry_ready"] for c in triggered),
                "float_only_blocked_states": sum(set(c["entry_block_reasons"]) ==
                    {"historical_float_unknown"} for c in triggered),
                "entry_block_counts": dict(Counter(reason for c in triggered
                    for reason in c["entry_block_reasons"])),
                "summary_sha256": summary["summary_sha256"]})
        blockers.update(actual_blockers)
        rvol_unavailable.update(summary["coverage"].get("strict_rvol_unavailable", {}))
        totals.update(counts)
        dates.setdefault(day, Counter()).update(counts)
        row = symbols.setdefault(symbol, {"counts": Counter(), "dates": [], "maximum_watched_rvol": None})
        row["counts"].update(counts)
        row["dates"].append(day)
        values = [c["rvol"] for c in candidates if c["rvol_valid"]]
        if values:
            row["maximum_watched_rvol"] = max(values + ([row["maximum_watched_rvol"]]
                if row["maximum_watched_rvol"] is not None else []))
        hashes.append(summary["summary_sha256"])
    return signed({"version": "candidate-session-readiness-v1", "plan_sha256": config["plan_sha256"],
        "symbol_scope": config.get("symbol_scope"),
        **({"session_scope": config["session_scope"]} if "session_scope" in config else {}),
        "generated_at": datetime.now(timezone.utc).isoformat(), "source_summary_hashes": hashes,
        "assembly_status": progress["status"], "planned_sessions": progress["total_sessions"],
        "remaining_sessions": progress["total_sessions"] - len(progress["completed"]),
        "totals": dict(totals), "unique_symbols": len(symbols), "entry_block_counts": dict(blockers),
        "all_state_rvol_unavailable_counts": dict(rvol_unavailable),
        "volume_repair_sessions": sorted(repair_sessions,
            key=lambda r: (-int(r["date"].replace("-", "")), -r["states"], r["symbol"])),
        "momentum_sessions": sorted(momentum_sessions, key=lambda r: (r["date"], r["symbol"]), reverse=True),
        "by_date": [{"date": day, **counts} for day, counts in sorted(dates.items(), reverse=True)],
        "by_symbol": [{"symbol": symbol, **row["counts"], "dates": row["dates"],
                       "maximum_watched_rvol": row["maximum_watched_rvol"]} for symbol, row in sorted(symbols.items())],
        "training_started": False, "network_calls": 0, "broker_calls": 0,
        "limitations": ["Readiness counts are not learned-policy performance or profitability",
            "Momentum states are repeated watch evaluations, not independent opportunities or trades",
            "Float-only blocked states still require valid dated float and execution-risk checks",
            "Volume repair leads pass other technical gates, not the unknown RVOL gate or unverified float",
            "This is a snapshot of completed sessions, not the unprocessed corpus",
            "Retain no-entry sessions; repair priority must not become return-based training selection"]}, "report_sha256")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.sessions)
    save(args.output, result)
    print(encode_dataset({k: v for k, v in result.items()
                          if k not in {"by_symbol", "source_summary_hashes", "volume_repair_sessions", "momentum_sessions"}}))


if __name__ == "__main__":
    main()
