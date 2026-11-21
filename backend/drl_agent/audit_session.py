"""Explain replay-window readiness without fitting a policy or changing data."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset
from drl_agent.partitions import inspect_session
from drl_agent.watch_replay import WatchConfig


def audit_sessions(sessions, *, watch_config):
    if not sessions or len(sessions) > 1000:
        raise ValueError("Provide 1..1000 chronological sessions")
    reports = []
    for datasets in sessions:
        row = inspect_session(datasets, watch_config)
        if reports and row["date"] <= reports[-1]["date"]:
            raise ValueError("Session dates must strictly increase")
        report = {k: v for k, v in row.items() if not k.startswith("_")}
        totals = {key: sum(c[key] for c in row["market_coverage"].values())
                  for key in next(iter(row["market_coverage"].values()))}
        watch_totals = {key: sum(c[key] for c in row["watch_market_coverage"].values())
                        for key in totals}
        blockers = []
        if not row["watch_symbol_ticks"]:
            blockers.append("no_qualifying_watch_candidates")
        for key, reason in (("usable_quote_states", "no_usable_quotes"),
                            ("fresh_valid_book_states", "no_fresh_valid_depth"),
                            ("fresh_candle_states", "no_fresh_completed_candles")):
            if not watch_totals[key]:
                blockers.append(reason)
        if not row["fillable_entry_probes"]:
            blockers.append("no_fillable_watch_entries")
        report.update(market_coverage_totals=totals, coverage_blockers=blockers,
                      watch_market_coverage_totals=watch_totals,
                      coverage_scope="watching_symbol_states",
                      basic_coverage_passed=not blockers)
        reports.append(report)
    result = {"version": "drl-session-audit-v2", "sessions": reports,
              "watch_config": asdict(watch_config), "training_ready": False,
              "training_started": False, "broker_calls": 0}
    result["audit_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        raise FileExistsError(args.output)
    manifest = json.loads(args.manifest.read_text())
    sessions = [[json.loads((args.manifest.parent / name).read_text()) for name in session]
                for session in manifest["sessions"]]
    result = audit_sessions(sessions, watch_config=WatchConfig(**manifest["watch_config"]))
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({**result, "sessions": [
        {k: v for k, v in session.items() if k not in {"market_coverage", "watch_market_coverage", "source_hashes", "symbols"}}
        for session in result["sessions"]]}))


if __name__ == "__main__":
    main()
