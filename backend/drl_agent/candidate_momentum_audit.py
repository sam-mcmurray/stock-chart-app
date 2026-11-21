"""Offline blocker inventory for indexed momentum sessions, not trade selection."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def audit(root: Path, index: dict) -> dict:
    root = root.resolve()
    _verify(index, "candidate-dataset-index-v1", "index_sha256")
    sessions = []
    totals = Counter()
    blockers = Counter()
    for identity, receipt in sorted(index["sessions"].items()):
        if not receipt["counts"]["momentum_states"]:
            continue
        path = (root / receipt["directory"] / "watch.json").resolve()
        if not path.is_relative_to(root):
            raise ValueError("Watch path escapes dataset root")
        watch = json.loads(path.read_text())
        _verify(watch, "historical-watch-v2", "watch_sha256")
        if watch["watch_sha256"] != receipt["watch_sha256"]:
            raise ValueError("Indexed watch changed")
        if (watch["source_hashes"] != {receipt["symbol"]: receipt["join_sha256"]}
                or watch["feature_hashes"] != {receipt["symbol"]: receipt["features_sha256"]}):
            raise ValueError("Indexed watch lineage mismatch")
        counts = Counter()
        reasons = Counter()
        float_only_times = []
        for frame in watch["frames"]:
            for state in frame["watching"]:
                if state["symbol"] != receipt["symbol"]:
                    raise ValueError("Unexpected symbol in indexed session")
                counts["watched_states"] += 1
                counts["entry_ready_states"] += int(state["research_entry_ready"])
                if not state["momentum_triggered"]:
                    continue
                counts["momentum_states"] += 1
                blocked = set(state["entry_block_reasons"])
                reasons.update(blocked)
                if blocked == {"historical_float_unknown"}:
                    float_only_times.append(frame["decision_ts"])
        for key in ("watched_states", "momentum_states", "entry_ready_states"):
            if counts[key] != receipt["counts"][key]:
                raise ValueError("Indexed watch counts mismatch")
        counts["float_only_states"] = len(float_only_times)
        totals.update(counts)
        blockers.update(reasons)
        sessions.append({
            "session": identity, "symbol": receipt["symbol"], "date": receipt["date"],
            "watch_sha256": receipt["watch_sha256"], "counts": dict(counts),
            "momentum_blockers": dict(sorted(reasons.items())),
            "float_only_decision_ts": float_only_times,
            "maximum_float": watch["config"]["maximum_float"],
        })
    if totals["momentum_states"] != index["totals"]["momentum_states"]:
        raise ValueError("Index momentum total mismatch")
    result = {
        "version": "candidate-momentum-audit-v1", "index_sha256": index["index_sha256"],
        "indexed_sessions": len(index["sessions"]), "audited_sessions": len(sessions),
        "totals": dict(totals), "momentum_blockers": dict(sorted(blockers.items())),
        "sessions": sorted(sessions, key=lambda row: (
            -row["counts"]["float_only_states"], -row["counts"]["momentum_states"], row["session"])),
        "network_calls": 0, "broker_calls": 0, "training_started": False,
        "limitations": [
            "Only indexed sessions with recorded momentum are inspected; other watch files are not audited.",
            "Counts are repeated state evaluations, not trades, independent samples or profitability evidence.",
            "Float-only means other recorded entry checks passed, not execution readiness or known low float.",
            "Dated float may exceed the configured cap; current float and shares outstanding are not substitutes.",
            "This repair-priority report must not select training or evaluation winners.",
        ],
    }
    result["audit_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = audit(args.root, json.loads(args.index.read_text()))
    with args.output.open("x") as output:
        output.write(encode_dataset(result) + "\n")
    print(encode_dataset({key: result[key] for key in
                         ("audited_sessions", "totals", "momentum_blockers", "audit_sha256")}))


if __name__ == "__main__":
    main()
