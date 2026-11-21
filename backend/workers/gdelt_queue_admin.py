import argparse
import json

from services.gdelt_event_queue import queue_status, replay_dead_letters


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or replay the canonical GDELT Event queue")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    replay = subparsers.add_parser("replay-dead-letters")
    replay.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    if args.command == "replay-dead-letters":
        replayed = replay_dead_letters(limit=max(0, args.limit))
        print(json.dumps({"replayed": replayed, **queue_status()}, sort_keys=True))
    else:
        print(json.dumps(queue_status(), sort_keys=True))


if __name__ == "__main__":
    main()
