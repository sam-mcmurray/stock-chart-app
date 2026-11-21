import argparse
import json

from services.calendar_relevance_queue import pending_counts, replay_failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the News LLM relevance queue")
    subparsers = parser.add_subparsers(dest="command", required=True)
    replay = subparsers.add_parser("replay-failed")
    replay.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if args.command == "replay-failed":
        replayed = replay_failed(limit=max(0, args.limit))
        print(json.dumps({"replayed": replayed, **pending_counts()}, sort_keys=True))


if __name__ == "__main__":
    main()
