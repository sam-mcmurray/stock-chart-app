"""Run fixed offline competitors without starting training or broker execution."""
import argparse
import json
from pathlib import Path

from drl_agent.competition import BaselinePolicy, compare_policies
from drl_agent.dataset import encode_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = compare_policies(
        [json.loads(Path(p).read_text()) for p in args.input],
        {"enter-hold": lambda: BaselinePolicy("enter-hold"),
         "enter-exit": lambda: BaselinePolicy("enter-exit")})
    with open(args.output, "x", encoding="utf-8") as output:
        output.write(encode_dataset(report))
    print(json.dumps({k: v for k, v in report.items() if k != "episodes"}, indent=2))


if __name__ == "__main__":
    main()
