"""Encode a local observation export into numeric DRL market features."""
import argparse
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset
from drl_agent.features import encode_observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.input.open(encoding="utf-8") as source:
        result = encode_observations(json.load(source))
    encoded = encode_dataset(result)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(encoded + "\n")
    print(encode_dataset(result["manifest"]))


if __name__ == "__main__":
    main()
