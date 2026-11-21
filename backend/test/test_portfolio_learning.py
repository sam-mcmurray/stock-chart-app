from copy import deepcopy
import os

import numpy as np
import pytest
import torch

from drl_agent.portfolio_learning import PortfolioEncoder, LearnedPolicy, Rollout, learn_session, make_agent
from drl_agent.portfolio_replay import PortfolioReplay
from drl_agent.train_portfolio import evaluate, save_checkpoint, restore_checkpoint, train_epoch
from test.test_drl_portfolio_replay import observations, WATCH


def setup_policy():
    torch.set_num_threads(1)
    torch.manual_seed(42)
    replay = PortfolioReplay([observations("A"), observations("B")], watch_config=WATCH)
    encoder = PortfolioEncoder(replay.simulators["A"].features[0]["names"], slots=3)
    return replay, encoder, make_agent(encoder)


def test_encoder_masks_padding_and_does_not_encode_future_or_ticker_identity():
    replay, encoder, _ = setup_policy()
    observation = replay.observation()
    state, mask, symbols = encoder.encode(observation)
    assert state.shape == (encoder.state_dim,)
    assert mask.tolist() == [True, True, False, False, True, False, False, False, False, False]
    assert symbols == [row["symbol"] for row in observation["watching"]]
    changed = deepcopy(observation)
    for index, row in enumerate(changed["watching"]):
        old = row["symbol"]
        row["symbol"] = f"OTHER{index}"
        changed["symbols"][row["symbol"]] = changed["symbols"].pop(old)
    np.testing.assert_array_equal(encoder.encode(changed)[0], state)
    with pytest.raises(ValueError, match="exceeds"):
        PortfolioEncoder(encoder.feature_names, slots=1).encode(observation)


def test_actual_rollout_updates_weights_and_saves_resumable_optimizer(tmp_path):
    replay, encoder, agent = setup_policy()
    before = {k: v.clone() for k, v in agent.policy.state_dict().items()}
    result = learn_session(replay, agent, encoder)
    assert result["transitions"] == 3
    assert any(not torch.equal(before[k], v) for k, v in agent.policy.state_dict().items())
    assert all(torch.isfinite(v).all() for v in agent.policy.state_dict().values())
    checkpoint = tmp_path / "checkpoint.pt"
    save_checkpoint(checkpoint, agent, {"identity": {"version": 1}, "episodes": 1})
    restored = make_agent(encoder)
    metadata = restore_checkpoint(checkpoint, restored, {"version": 1})
    assert metadata["episodes"] == 1
    for key, value in agent.policy.state_dict().items():
        assert torch.equal(value, restored.policy_old.state_dict()[key])
    assert restored.optimizer.state_dict()["state"]
    with pytest.raises(ValueError, match="changed"):
        restore_checkpoint(checkpoint, restored, {"version": 2})


def test_single_transition_update_is_finite():
    replay, encoder, agent = setup_policy()
    replay.step({})
    replay.step({})
    assert learn_session(replay, agent, encoder)["transitions"] == 1
    assert all(torch.isfinite(v).all() for v in agent.policy.state_dict().values())


def test_legacy_unmasked_rollout_still_updates():
    replay, encoder, agent = setup_policy()
    state, _, _ = encoder.encode(replay.observation())
    memory = Rollout()
    agent.select_action(state, memory)
    memory.rewards.append(0.01)
    memory.is_terminals.append(True)
    agent.update(memory)
    assert all(torch.isfinite(v).all() for v in agent.policy.state_dict().values())


@pytest.mark.parametrize("field,value", [("rvol", 2), ("float_shares", None), ("float_shares", 30_000_000)])
def test_replay_blocks_waiting_candidate_entry(field, value):
    source = observations("A")
    for state in source["states"]:
        for item in state["inputs"]["scanner"]:
            item["record"]["payload"]["watch_inputs"][field] = value
    replay = PortfolioReplay([source], watch_config=WATCH)
    assert replay.observation()["watching"]
    _, _, _, _, event = replay.step({"A": 1})
    assert event["symbols"]["A"]["fill"] is None
    assert not replay.account()["positions"]


def test_evaluation_does_not_train_and_reports_checkpoint_and_weekly_capital():
    _, encoder, agent = setup_policy()
    before = {k: v.clone() for k, v in agent.policy.state_dict().items()}
    report = evaluate([[observations("A"), observations("B")]], WATCH, agent, encoder,
                      checkpoint_sha256="test-checkpoint", split="test")
    assert report["checkpoint_sha256"] == "test-checkpoint"
    assert report["evaluation_split"] == "test"
    assert report["weekly_starting_cash"] == 500
    assert report["broker_calls"] == 0 and report["promotion_allowed"] is False
    assert set(report["competitors"]) == {"learned-ppo", "enter-exit", "stay-out"}
    for key, value in agent.policy.state_dict().items():
        assert torch.equal(value, before[key])


def test_policy_empty_watchlist_can_only_hold():
    replay, encoder, agent = setup_policy()
    observation = replay.observation()
    observation["watching"] = []
    observation["symbols"] = {}
    assert LearnedPolicy(agent, encoder)(observation) == {}


def test_training_epoch_records_real_updates():
    _, encoder, agent = setup_policy()
    results = train_epoch([[observations("A"), observations("B")]], WATCH, agent, encoder)
    assert len(results) == 1
    assert results[0]["starting_cash"] == 500
    assert results[0]["transitions"] == 3


def test_mask_retains_waiting_stock_but_blocks_entry():
    replay, encoder, _ = setup_policy()
    observation = replay.observation()
    observation["watching"][0]["entry_block_reasons"] = ["rvol_below_threshold"]
    state, mask, symbols = encoder.encode(observation)
    assert len(symbols) == 2
    assert not mask[1]
    assert mask[4]


def test_cli_checkpoint_resume_and_reserved_test_split(tmp_path, monkeypatch):
    import json
    from dataclasses import asdict
    from drl_agent.dataset import encode_dataset
    from drl_agent.train_portfolio import main
    from test.test_drl_partitions import day
    for i in range(3):
        (tmp_path / f"day{i}.json").write_text(encode_dataset(day(i)))
    manifest = tmp_path / "sessions.json"
    manifest.write_text(json.dumps({"sessions": [[f"day{i}.json"] for i in range(3)],
                                    "watch_config": asdict(WATCH)}))
    output = tmp_path / "run"
    argv = ["train_portfolio", "--manifest", str(manifest), "--train-end", "2026-09-22",
            "--validation-end", "2026-09-23", "--embargo-seconds", "0",
            "--slots", "2", "--threads", "1", "--output", str(output)]
    monkeypatch.setattr("sys.argv", [*argv, "--epochs", "2"])
    main()
    first = json.loads((output / "progress.json").read_text())
    assert first["episodes"] == 2 and first["transitions"] == 6
    assert not (output / "test-performance.json").exists()
    monkeypatch.setattr("sys.argv", [*argv, "--epochs", "3", "--resume", "--evaluate-test"])
    main()
    last = json.loads((output / "progress.json").read_text())
    assert last["episodes"] == 3 and last["transitions"] == 9
    report = json.loads((output / "test-performance.json").read_text())
    assert report["evaluation_split"] == "test"
    assert report["checkpoint_sha256"]


@pytest.mark.skipif(os.getenv("DRL_STRESS_TEST") != "1", reason="Explicit training-loop stress test")
def test_thousand_learning_episodes(tmp_path):
    replay, encoder, agent = setup_policy()
    transitions, fills = 0, 0
    for _ in range(1000):
        replay.reset()
        result = learn_session(replay, agent, encoder)
        transitions += result["transitions"]
        fills += result["fills"]
    save_checkpoint(tmp_path / "fixture-only.pt", agent,
                    {"identity": {"fixture_only": True}, "episodes": 1000})
    assert transitions == 3000
    assert all(torch.isfinite(v).all() for v in agent.policy.state_dict().values())
    print({"fixture_only": True, "learning_episodes": 1000,
           "transitions": transitions, "fills": fills, "broker_calls": 0})
