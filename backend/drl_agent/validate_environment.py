"""Validate a local observation dataset through Gymnasium without training."""
import argparse
import json
from pathlib import Path

from gymnasium.utils.env_checker import check_env

from drl_agent.dataset import encode_dataset
from drl_agent.simulator import Action
from drl_agent.training_env import NewsTradingEnv, PORTFOLIO_NAMES, VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    with args.input.open(encoding="utf-8") as source:
        env = NewsTradingEnv(json.load(source))
    check_env(env, skip_render_check=True)
    observation, _ = env.reset(seed=0)
    steps = 0
    while True:
        if not env.observation_space.contains(observation):
            raise ValueError("Observation outside declared space")
        if env.simulator.done:
            break
        observation, _, _, _, _ = env.step(Action.HOLD)
        steps += 1
    print(encode_dataset({"version": VERSION, "gymnasium_check": "passed",
                          "market_features": len(env.feature_names),
                          "portfolio_features": len(PORTFOLIO_NAMES),
                          "checked_transitions": steps, "broker_calls": 0,
                          "training_started": False}))
    env.close()


if __name__ == "__main__":
    main()
