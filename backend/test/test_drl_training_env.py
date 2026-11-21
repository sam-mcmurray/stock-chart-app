import numpy as np
import pytest

pytest.importorskip("gymnasium", reason="Install requirements-training.txt for training tests")
from gymnasium.utils.env_checker import check_env

from drl_agent.training_env import NewsTradingEnv, PORTFOLIO_NAMES
from drl_agent.simulator import Action
from test.test_drl_simulator import dataset


def test_gymnasium_contract_and_seeded_reset():
    env = NewsTradingEnv(dataset())
    check_env(env, skip_render_check=True)
    first, _ = env.reset(seed=42)
    result = env.step(Action.ENTER)
    second, _ = env.reset(seed=42)
    for key in first:
        np.testing.assert_array_equal(first[key], second[key])
    replay = env.step(Action.ENTER)
    for key in result[0]:
        np.testing.assert_array_equal(result[0][key], replay[0][key])
    assert result[1:] == replay[1:]


def test_portfolio_changes_and_structural_action_masks():
    env = NewsTradingEnv(dataset())
    observation, _ = env.reset()
    assert env.observation_space.contains(observation)
    assert observation["action_mask"].tolist() == [1, 1, 0, 0]
    observation, *_ = env.step(Action.ENTER)
    assert env.observation_space.contains(observation)
    assert observation["action_mask"].tolist() == [1, 0, 1, 1]
    portfolio = dict(zip(PORTFOLIO_NAMES, observation["portfolio"]))
    assert portfolio["position_present"] == 1
    assert portfolio["exposure"] > 0
    assert portfolio["open_risk"] > 0
    assert portfolio["cash"] < 0.5
    observation, *_ = env.step(Action.TAKE_PARTIAL)
    assert observation["action_mask"].tolist() == [1, 0, 0, 1]


def test_truncation_keeps_open_position_and_returns_independent_arrays():
    env = NewsTradingEnv(dataset())
    old, _ = env.reset()
    old["market"].fill(999)
    observation, *_ = env.step(Action.ENTER)
    assert env.observation_space.contains(observation)
    env.step(Action.HOLD)
    observation, reward, terminated, truncated, info = env.step(Action.HOLD)
    assert not terminated and truncated and info["open_at_end"]
    assert env.observation_space.contains(observation)
    assert np.isfinite(reward)
    with pytest.raises(RuntimeError):
        env.step(Action.HOLD)


def test_requires_reset_and_rejects_invalid_actions():
    env = NewsTradingEnv(dataset())
    with pytest.raises(RuntimeError):
        env.step(0)
    env.reset()
    for invalid in (-1, 4, 1.5, True):
        with pytest.raises(ValueError):
            env.step(invalid)
