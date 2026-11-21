"""Offline independent policy comparisons; competitors cannot alter replay rules."""
from copy import deepcopy
from dataclasses import asdict
import hashlib

from drl_agent.dataset import encode_dataset
from drl_agent.simulator import Action, SimulationConfig, VERSION as SIMULATOR_VERSION
from drl_agent.training_env import NewsTradingEnv, PORTFOLIO_NAMES, VERSION as ENV_VERSION

VERSION = "drl-competition-v1"


class BaselinePolicy:
    def __init__(self, mode):
        if mode not in {"stay-out", "enter-hold", "enter-exit"}:
            raise ValueError("Unknown baseline")
        self.mode = mode

    def __call__(self, observation):
        if self.mode == "stay-out":
            return Action.HOLD
        holding = observation["portfolio"][PORTFOLIO_NAMES.index("position_present")]
        if holding:
            return Action.EXIT if self.mode == "enter-exit" else Action.HOLD
        return Action.ENTER


def compare_policies(datasets, policy_factories, *, config=None, seed=0):
    """Factories create fresh trusted policies for each independent episode.

    Policies receive only copied current observations, never the simulator,
    future states, opponent actions, or end-of-file countdown. This is an API
    boundary, not a sandbox for untrusted Python or model-loading code.
    """
    if not datasets or len(policy_factories) < 2 or "stay-out" in policy_factories:
        raise ValueError("Provide datasets and at least two competitors; stay-out is reserved")
    config = config or SimulationConfig()
    factories = {"stay-out": lambda: BaselinePolicy("stay-out"), **policy_factories}
    episodes = {name: [] for name in factories}
    fingerprints = set()
    provenance = []
    for dataset in datasets:
        # Hash actual input, not an unchecked digest supplied by its manifest.
        digest = hashlib.sha256(encode_dataset(dataset).encode("utf-8")).hexdigest()
        if digest in fingerprints:
            raise ValueError("Duplicate episode supplied")
        fingerprints.add(digest)
        provenance.append({"input_sha256": digest,
                           "source_dataset_sha256": dataset["manifest"].get("dataset_sha256")})
        for name, factory in factories.items():
            env = NewsTradingEnv(dataset, config=config)
            policy = factory()
            observation, _ = env.reset(seed=seed)
            total_reward = 0.0
            done = False
            while not done:
                action = policy(deepcopy(observation))
                observation, reward, terminated, truncated, _ = env.step(action)
                total_reward += reward
                done = terminated or truncated
            s = env.simulator
            episodes[name].append({
                "input_sha256": digest, "symbol": s.states[0]["symbol"],
                "start_ts": s.states[0]["decision_ts"], "end_ts": s.states[-1]["decision_ts"],
                "net_return": (s.equity - config.initial_cash) / config.initial_cash,
                "risk_adjusted_score": total_reward,
                "max_drawdown_fraction": s.max_drawdown / config.initial_cash,
                "fees": s.fees, "fills": sum(e["fill"] is not None for e in s.events),
                "open_quantity": s.quantity, "pending_sell": s.pending_sell,
                "stale_valuation_steps": sum(e["valuation_stale"] for e in s.events),
                "events": deepcopy(s.events),
            })
            env.close()
    summaries = {}
    for name, rows in episodes.items():
        summaries[name] = {
            "mean_net_return": sum(r["net_return"] for r in rows) / len(rows),
            "mean_risk_adjusted_score": sum(r["risk_adjusted_score"] for r in rows) / len(rows),
            "worst_episode_drawdown": max(r["max_drawdown_fraction"] for r in rows),
            "positive_episode_fraction": sum(r["net_return"] > 0 for r in rows) / len(rows),
            "fills": sum(r["fills"] for r in rows),
        }
    warnings = []
    if any(r["open_quantity"] for rows in episodes.values() for r in rows):
        warnings.append("unresolved_positions_at_truncation")
    if any(r["stale_valuation_steps"] for rows in episodes.values() for r in rows):
        warnings.append("stale_position_valuations")
    if not any(s["fills"] for s in summaries.values()):
        warnings.append("no_executions_comparison_is_uninformative")
    # Keep all numerical comparisons descriptive. No test split or minimum
    # evidence requirement has been certified by this runner.
    return {"version": VERSION, "simulator_version": SIMULATOR_VERSION,
            "environment_version": ENV_VERSION,
            "risk_limits": asdict(s.risk), "exit_policy": asdict(s.policy),
            "config": asdict(config), "seed": seed,
            "provenance": provenance, "summaries": summaries, "episodes": episodes,
            "warnings": warnings, "evaluation_only": True, "promotion_allowed": False,
            "training_started": False, "broker_calls": 0}
