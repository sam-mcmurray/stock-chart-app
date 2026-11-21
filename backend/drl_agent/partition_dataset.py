"""Validate and partition a local portfolio session manifest without training."""
import argparse
import hashlib
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset
from drl_agent.partitions import partition_sessions
from drl_agent.watch_replay import WatchConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--validation-end", required=True)
    parser.add_argument("--embargo-seconds", type=int, default=86400)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    manifest = json.loads(args.manifest.read_text())
    sessions = [[json.loads((args.manifest.parent / name).read_text()) for name in session]
                for session in manifest["sessions"]]
    result = partition_sessions(sessions, watch_config=WatchConfig(**manifest["watch_config"]),
                                train_end=args.train_end, validation_end=args.validation_end,
                                embargo_seconds=args.embargo_seconds)
    result["partition_manifests"] = {
        split: {"watch_config": manifest["watch_config"],
                "sessions": [[str((args.manifest.parent / name).resolve()) for name in manifest["sessions"][row["session_index"]]]
                             for row in rows]}
        for split, rows in result["partitions"].items()}
    result.pop("partition_sha256")
    result["partition_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    with args.output.open("x", encoding="utf-8") as output:
        output.write(encode_dataset(result) + "\n")
    print(encode_dataset({k: result[k] for k in ("version", "coverage", "coverage_blockers", "training_ready")}))


if __name__ == "__main__":
    main()
