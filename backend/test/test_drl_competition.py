from copy import deepcopy

import pytest

pytest.importorskip("gymnasium")

from drl_agent.competition import BaselinePolicy, compare_policies
from drl_agent.simulator import Action
from test.test_drl_simulator import dataset


def competitors():
    return {"a": lambda: BaselinePolicy("enter-exit"),
            "b": lambda: BaselinePolicy("enter-exit")}


def test_identical_agents_equal_conditions_and_reproducibility():
    source = dataset()
    before = deepcopy(source)
    report = compare_policies([source], competitors())
    assert report == compare_policies([source], competitors())
    assert source == before
    assert report["episodes"]["a"] == report["episodes"]["b"]
    assert report["summaries"]["a"]["fills"] > 0
    assert report["episodes"]["a"][0]["fees"] > 0
    assert not report["promotion_allowed"] and report["broker_calls"] == 0


def test_staying_out_beats_losing_trader_without_activity_bonus():
    report = compare_policies([dataset((10, 10, 9))], competitors())
    assert report["summaries"]["stay-out"]["mean_risk_adjusted_score"] == 0
    assert report["summaries"]["a"]["mean_risk_adjusted_score"] < 0
    assert report["episodes"]["a"][0]["events"][-1]["reason"] == "hard_stop"


def test_policy_only_gets_current_copied_observation():
    seen = []
    def factory():
        def policy(obs):
            assert set(obs) == {"market", "market_valid", "portfolio", "action_mask"}
            seen.append(obs["market"].copy())
            obs["market"][:] = 99
            return Action.HOLD
        return policy
    report = compare_policies([dataset()], {"mutator": factory,
                                           "plain": lambda: BaselinePolicy("stay-out")})
    assert report["episodes"]["mutator"] == report["episodes"]["plain"]
    assert all((abs(row) <= 1).all() for row in seen)


def test_no_fills_and_unresolved_stale_positions_are_explicit():
    report = compare_policies([dataset(book=False)], competitors())
    assert "no_executions_comparison_is_uninformative" in report["warnings"]
    report = compare_policies([dataset((10, 10, 9, 9, 9, 9), stale_at=(2, 3, 4, 5))], competitors())
    assert "unresolved_positions_at_truncation" in report["warnings"]
    assert "stale_position_valuations" in report["warnings"]


def test_factories_reset_each_episode_and_duplicate_inputs_rejected():
    calls = []
    def factory():
        calls.append(1)
        return BaselinePolicy("stay-out")
    compare_policies([dataset(), dataset((10, 10, 9))],
                     {"fresh": factory, "b": lambda: BaselinePolicy("stay-out")})
    assert len(calls) == 2
    with pytest.raises(ValueError, match="Duplicate"):
        compare_policies([dataset(), dataset()], competitors())
    with pytest.raises(ValueError):
        compare_policies([dataset()], {"a": lambda: lambda obs: 99, "b": factory})
