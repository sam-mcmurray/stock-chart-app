"""Run a deterministic offline baseline; this command does not train a policy."""
import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset
from drl_agent.simulator import Action, TradingSimulator, VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", choices=("hold", "enter-hold"), default="hold")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    with args.input.open(encoding="utf-8") as source:
        dataset = json.load(source)
    simulator = TradingSimulator(dataset)
    while not simulator.done:
        action = Action.ENTER if args.policy == "enter-hold" and not simulator.quantity else Action.HOLD
        simulator.step(action)
    report = {
        "version": VERSION, "policy": args.policy,
        "source_dataset_sha256": dataset["manifest"]["dataset_sha256"],
        "config": asdict(simulator.config), "risk_limits": asdict(simulator.risk),
        "exit_policy": asdict(simulator.policy),
        "step_count": len(simulator.events),
        "fill_count": sum(e["fill"] is not None for e in simulator.events),
        "reason_counts": dict(Counter(e["reason"] for e in simulator.events)),
        "cash": simulator.cash, "equity": simulator.equity, "realized_pnl": simulator.realized,
        "fees": simulator.fees, "maximum_drawdown": simulator.max_drawdown,
        "open_quantity": simulator.quantity, "pending_sell": simulator.pending_sell,
        "final_valuation_stale": simulator.events[-1]["valuation_stale"],
        "total_reward": sum(e["reward"] for e in simulator.events),
        "broker_calls": 0, "live_activation_eligible": False,
        "limitations": ["sampled_decision_grid", "single_symbol_single_session",
                        "displayed_liquidity_not_guaranteed_fill", "no_intra_step_stop_reconstruction"],
    }
    with args.output.open("x", encoding="utf-8") as output:
        output.write(encode_dataset({"report": report, "events": simulator.events}) + "\n")
    print(encode_dataset(report))


if __name__ == "__main__":
    main()
