"""Run daily/weekly cash-only baseline comparisons from a local session manifest."""
import argparse
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset
from drl_agent.portfolio_competition import EnterExitPolicy, compare_portfolio_sessions
from drl_agent.simulator import Action
from drl_agent.watch_replay import WatchConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    manifest = json.loads(args.manifest.read_text())
    sessions = [[json.loads((args.manifest.parent / filename).read_text()) for filename in session]
                for session in manifest["sessions"]]
    report = compare_portfolio_sessions(sessions,
        {"enter-exit": EnterExitPolicy,
         "enter-hold": lambda: lambda obs: {c["symbol"]: Action.ENTER for c in obs["watching"]
                                            if c["symbol"] not in obs["account"]["positions"]}},
        watch_config=WatchConfig(**manifest["watch_config"]))
    with args.output.open("x", encoding="utf-8") as output:
        output.write(encode_dataset(report) + "\n")
    print(encode_dataset(report))


if __name__ == "__main__":
    main()
