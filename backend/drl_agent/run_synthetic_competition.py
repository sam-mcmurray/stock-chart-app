"""Compare explicit diagnostic order schedules using local synthetic artifacts."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from drl_agent.dataset import encode_dataset
from drl_agent.synthetic_competition import compare_synthetic_sessions
from drl_agent.synthetic_portfolio_competition import compare_synthetic_portfolio_sessions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--portfolio", action="store_true", help="Shared account; joined paths are arrays and orders include symbol")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    manifest = json.loads(args.manifest.read_text())
    sessions = []
    for session in manifest["sessions"]:
        paths = session["joined"] if args.portfolio else [session["joined"]]
        if not isinstance(paths, list) or not paths:
            raise ValueError("Portfolio sessions require an array of joined paths")
        datasets = [json.loads((args.manifest.parent / path).read_text()) for path in paths]
        sessions.append({"date": session["date"], "joined": datasets if args.portfolio else datasets[0]})
        if session.get("watch"):
            if not args.portfolio:
                raise ValueError("Historical watch gating requires --portfolio")
            sessions[-1]["watch"] = json.loads((args.manifest.parent / session["watch"]).read_text())
    allowed = set()
    for session in sessions:
        datasets = session["joined"] if args.portfolio else [session["joined"]]
        for dataset in datasets:
            states = [s for s in dataset["states"]
                      if datetime.fromtimestamp(s["decision_ts"], ZoneInfo("America/New_York")).date().isoformat()
                      == session["date"]]
            allowed.update((s["decision_ts"], dataset["symbol"] if args.portfolio else None) for s in states[:-1])
    factories = {}
    for name, orders in manifest["competitors"].items():
        planned = {}
        seen = set()
        for raw in orders:
            order = dict(raw)
            timestamp = order.pop("decision_ts")
            symbol = order.pop("symbol") if args.portfolio else None
            identity = (timestamp, symbol)
            if type(timestamp) is not int or identity not in allowed or identity in seen:
                raise ValueError("Invalid or duplicate order decision timestamp")
            seen.add(identity)
            if args.portfolio:
                planned.setdefault(timestamp, {})[symbol] = order
            else:
                planned[timestamp] = order
        factories[name] = lambda schedule=planned: lambda obs: deepcopy(schedule.get(
            obs["decision_ts"] if args.portfolio else obs["state"]["decision_ts"], {}))
    compare = compare_synthetic_portfolio_sessions if args.portfolio else compare_synthetic_sessions
    report = compare(sessions, factories)
    # Preserve the full diagnostic schedule separately from actual fills.
    report["diagnostic_orders"] = manifest["competitors"]
    report.pop("report_sha256")
    report["report_sha256"] = hashlib.sha256(encode_dataset(report).encode()).hexdigest()
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(report) + "\n")
    print(encode_dataset({"version": report["version"], "report_sha256": report["report_sha256"],
                         "competitors": {n: {"complete": r["complete"], "warnings": r["warnings"],
                                             "weekly": r["weekly"]}
                                         for n, r in report["competitors"].items()}}))


if __name__ == "__main__":
    main()
