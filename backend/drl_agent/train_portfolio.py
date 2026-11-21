"""Train offline PPO on purged chronological Scanner sessions, without broker calls."""
import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from drl_agent.dataset import encode_dataset
from drl_agent.partitions import partition_sessions
from drl_agent.portfolio_competition import EnterExitPolicy, compare_portfolio_sessions
from drl_agent.portfolio_replay import PortfolioReplay
from drl_agent.simulator import SimulationConfig
from drl_agent.watch_replay import WatchConfig


def write_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encode_dataset(payload) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare(manifest_path, train_end, validation_end, embargo_seconds):
    manifest = json.loads(manifest_path.read_text())
    watch = WatchConfig(**manifest["watch_config"])
    sessions = [[json.loads((manifest_path.parent / name).read_text()) for name in session]
                for session in manifest["sessions"]]
    partition = partition_sessions(sessions, watch_config=watch, train_end=train_end,
                                   validation_end=validation_end, embargo_seconds=embargo_seconds)
    if not partition["basic_coverage_passed"]:
        raise ValueError("Training data blocked: " + ", ".join(partition["coverage_blockers"]))
    groups = {split: [sessions[row["session_index"]] for row in rows]
              for split, rows in partition["partitions"].items()}
    names = None
    for session in sessions:
        replay = PortfolioReplay(session, watch_config=watch)
        for sim in replay.simulators.values():
            for features in sim.features:
                schema = tuple(features["names"])
                if names is not None and schema != names:
                    raise ValueError("Mixed feature schemas")
                names = schema
    return groups, watch, partition, names


def train_epoch(sessions, watch, agent, encoder, *, replay_factory=PortfolioReplay):
    from drl_agent.portfolio_learning import learn_session
    cash, history, previous_week = 500.0, [], None
    results = []
    for session in sessions:
        date = (datetime.strptime(session["date"], "%Y-%m-%d").date() if isinstance(session, dict) else
                datetime.fromtimestamp(session[0]["states"][0]["decision_ts"],
                                       ZoneInfo("America/New_York")).date())
        week = date.isocalendar()[:2]
        if previous_week is not None and week != previous_week:
            cash = 500.0
        previous_week = week
        replay = replay_factory(session, watch_config=watch,
                                 config=replace(SimulationConfig(), initial_cash=cash),
                                 prior_round_trip_timestamps=history)
        result = learn_session(replay, agent, encoder)
        results.append({"date": date.isoformat(), "starting_cash": cash, **result})
        if result["unresolved_position"]:
            break
        cash, history = replay.account()["cash"], replay.round_trip_timestamps
        if cash <= 0:
            break
    return results


def save_checkpoint(path, agent, metadata):
    import torch
    temporary = path.with_suffix(".tmp")
    torch.save({"metadata": metadata, "policy": agent.policy.state_dict(),
                "optimizer": agent.optimizer.state_dict(), "rng": torch.get_rng_state()}, temporary)
    temporary.replace(path)


def restore_checkpoint(path, agent, identity):
    import torch
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if checkpoint["metadata"]["identity"] != identity:
        raise ValueError("Checkpoint dataset, feature schema or policy configuration changed")
    agent.policy.load_state_dict(checkpoint["policy"])
    agent.policy_old.load_state_dict(checkpoint["policy"])
    agent.optimizer.load_state_dict(checkpoint["optimizer"])
    torch.set_rng_state(checkpoint["rng"])
    return checkpoint["metadata"]


def evaluate(sessions, watch, agent, encoder, *, checkpoint_sha256, split, replay_factory=PortfolioReplay):
    from drl_agent.portfolio_learning import VERSION, LearnedPolicy
    result = compare_portfolio_sessions(sessions, {
        "learned-ppo": lambda: LearnedPolicy(agent, encoder), "enter-exit": EnterExitPolicy,
    }, watch_config=watch, replay_factory=replay_factory)
    historical = replay_factory is not PortfolioReplay
    result.update(training_started=True, evaluation_split=split, policy_version=VERSION,
                  synthetic=historical, retrospective=historical,
                  checkpoint_sha256=checkpoint_sha256,
                  limitations=["One discretionary symbol action per decision tick",
                               "Synthetic minute-close depth; retrospective news" if historical else "Recorded quote/book replay",
                               "Open positions stop subsequent sessions; no fabricated liquidation"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--validation-end", required=True)
    parser.add_argument("--embargo-seconds", type=int, default=86400)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=1000, help="Total epochs including resumed work")
    parser.add_argument("--slots", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--historical", action="store_true", help="Use explicitly synthetic historical replay")
    parser.add_argument("--audit-only", action="store_true", help="Write historical data coverage without training")
    parser.add_argument("--evaluate-test", action="store_true",
                        help="Evaluate the frozen final checkpoint on the reserved test split")
    args = parser.parse_args()
    if args.epochs < 1 or not 1 <= args.threads <= 32:
        parser.error("epochs must be positive; threads must be 1..32")
    if args.output.exists() and not args.resume:
        parser.error("Output exists; use --resume or a new output directory")
    replay_factory = PortfolioReplay
    if args.historical:
        from drl_agent.historical_learning import HistoricalLearningReplay
        from drl_agent.historical_training_data import prepare_historical
        replay_factory = HistoricalLearningReplay
        groups, watch, partition, names = prepare_historical(args.manifest, args.train_end,
            args.validation_end, args.embargo_seconds, require_coverage=not args.audit_only)
    else:
        if args.audit_only:
            parser.error("--audit-only currently requires --historical")
        groups, watch, partition, names = prepare(args.manifest, args.train_end,
                                                  args.validation_end, args.embargo_seconds)
    if args.audit_only:
        if args.resume:
            parser.error("An audit cannot resume a training run")
        args.output.mkdir(parents=True, exist_ok=False)
        write_json(args.output / "partitions.json", partition)
        print(encode_dataset({"training_started": False, "coverage": partition["coverage"],
                              "coverage_blockers": partition["coverage_blockers"]}), flush=True)
        return
    import torch
    from drl_agent.portfolio_learning import VERSION, PortfolioEncoder, make_agent
    torch.set_num_threads(args.threads)
    torch.manual_seed(args.seed)
    encoder = PortfolioEncoder(names, args.slots)
    agent = make_agent(encoder)
    identity = {"policy_version": VERSION, "partition_sha256": partition["partition_sha256"],
                "feature_names": list(names), "slots": args.slots, "seed": args.seed,
                "replay_mode": "historical-synthetic" if args.historical else "recorded"}
    checkpoint = args.output / "checkpoint.pt"
    metadata = {"identity": identity, "completed_epochs": 0, "episodes": 0,
                "transitions": 0, "fills": 0, "training_started": True,
                "broker_calls": 0, "promotion_allowed": False}
    if args.resume:
        metadata = restore_checkpoint(checkpoint, agent, identity)
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "partitions.json", partition)
    for epoch in range(metadata["completed_epochs"], args.epochs):
        results = train_epoch(groups["train"], watch, agent, encoder, replay_factory=replay_factory)
        metadata.update(completed_epochs=epoch + 1,
                        status="training", updated_at=datetime.now(ZoneInfo("UTC")).isoformat(),
                        episodes=metadata["episodes"] + len(results),
                        transitions=metadata["transitions"] + sum(r["transitions"] for r in results),
                        fills=metadata["fills"] + sum(r["fills"] for r in results),
                        unique_available_training_dates=len(groups["train"]),
                        latest_epoch=results,
                        interrupted_by_open_position=bool(results[-1]["unresolved_position"]))
        save_checkpoint(checkpoint, agent, metadata)
        write_json(args.output / "progress.json", metadata)
        print(encode_dataset({key: metadata[key] for key in
                              ("completed_epochs", "episodes", "transitions", "fills",
                               "interrupted_by_open_position")}), flush=True)
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    metadata.update(status="evaluating", updated_at=datetime.now(ZoneInfo("UTC")).isoformat())
    write_json(args.output / "progress.json", metadata)
    # Neither held-out split is used by the optimizer or checkpoint selection.
    for split in (("validation", "test") if args.evaluate_test else ("validation",)):
        report = evaluate(groups[split], watch, agent, encoder, checkpoint_sha256=digest, split=split,
                          replay_factory=replay_factory)
        write_json(args.output / (split + "-performance.json"), report)
    metadata.update(status="completed", updated_at=datetime.now(ZoneInfo("UTC")).isoformat())
    write_json(args.output / "progress.json", metadata)


if __name__ == "__main__":
    main()
