from copy import deepcopy
from dataclasses import asdict
import json
import os

import numpy as np
import pytest
import torch

from drl_agent.dataset import encode_dataset
from drl_agent.historical_features import build_features
from drl_agent.historical_learning import FEATURE_NAMES, HistoricalLearningReplay
from drl_agent.historical_training_data import prepare_historical
from drl_agent.historical_watch import build_watch
from drl_agent.portfolio_learning import PortfolioEncoder, learn_session, make_agent
from drl_agent.train_portfolio import evaluate
from test.test_historical_watch import CONFIG
from test.test_synthetic_execution import fixture, resign


def session(day="2018-07-16", *, float_known=True, rvol=4, evidence_id=None):
    joined = fixture(day=day, symbol="A")
    timestamp = joined["states"][0]["decision_ts"]
    identity = evidence_id or day + "-news"
    joined["articles"] = [{"candidate_id": identity, "replay_available_ts": timestamp}]
    for state in joined["states"]:
        state["news_candidate_ids"] = [identity]
    resign(joined, "join_sha256")
    features = build_features(joined)
    # Measured inputs are explicitly invented ONLY for this tiny mechanics fixture.
    for row in features["states"]:
        row.update(rvol=rvol, rvol_valid=True, bar_vwap_estimate=10,
                   minute_macd=.2, minute_macd_signal=.1)
    resign(features, "features_sha256")
    evidence = [dict(symbol="A", kind="price", value=10.5, basis="nominal", source="test",
                     effective_ts=timestamp, available_ts=timestamp, valid_until_ts=timestamp + 86400)]
    if float_known:
        evidence.append({**evidence[0], "kind": "float", "value": 1_000_000, "basis": "float_shares"})
    watch = build_watch([joined], [features], evidence, config=CONFIG)
    return {"date": day, "joined": [joined], "features": [features], "watch": watch}, evidence


def test_adapter_learning_update_and_synthetic_evaluation():
    torch.set_num_threads(1)
    torch.manual_seed(42)
    data, _ = session()
    replay = HistoricalLearningReplay(data)
    encoder = PortfolioEncoder(FEATURE_NAMES, slots=2)
    agent = make_agent(encoder)
    before = {key: value.clone() for key, value in agent.policy.state_dict().items()}
    result = learn_session(replay, agent, encoder)
    assert result["transitions"] == 7
    assert any(not torch.equal(before[k], v) for k, v in agent.policy.state_dict().items())
    assert all(row["fill"]["synthetic"] for e in replay.events for row in e["symbols"].values() if row["fill"])
    report = evaluate([data], CONFIG, agent, encoder, checkpoint_sha256="test", split="validation",
                      replay_factory=HistoricalLearningReplay)
    assert report["synthetic"] and report["retrospective"]
    assert report["broker_calls"] == 0 and not report["promotion_allowed"]


def test_entry_exit_rewards_and_no_future_observation():
    data, _ = session()
    replay = HistoricalLearningReplay(data)
    initial = replay.observation()
    # Mutate an internal future snapshot: current features must not read it.
    future = replay.engine.simulators["A"].books[replay.engine.simulators["A"].states[-1]["candle_id"]]
    future["bids"][0]["price"] = 1
    assert replay.observation() == initial
    _, _, _, _, entry = replay.step({"A": 1})
    assert entry["symbols"]["A"]["fill"]["side"] == "buy"
    _, _, _, _, exit_event = replay.step({"A": 3})
    assert exit_event["symbols"]["A"]["fill"]["side"] == "sell"
    assert len(replay.round_trip_timestamps) == 1
    account = replay.account()
    assert sum(e["reward"] for e in replay.events) == pytest.approx(
        (account["equity"] - 500 - .1 * account["max_drawdown"]) / 500)


@pytest.mark.parametrize("kwargs", [{"float_known": False}, {"rvol": 1}])
def test_unknown_float_or_low_rvol_still_watch_but_never_enter(kwargs):
    data, _ = session(**kwargs)
    replay = HistoricalLearningReplay(data)
    encoder = PortfolioEncoder(FEATURE_NAMES, slots=1)
    state, mask, symbols = encoder.encode(replay.observation())
    assert symbols == ["A"] and not mask[1]
    assert np.isfinite(state).all()
    event = replay.step({"A": 1})[-1]
    assert event["symbols"]["A"]["fill"] is None


def manifest(tmp_path, *, shared=False, float_known=True):
    rows = []
    for day in ("2018-07-16", "2018-07-23", "2018-07-30"):
        data, evidence = session(day, float_known=float_known, evidence_id="same-news" if shared else None)
        for suffix, payload in (("joined", data["joined"][0]), ("features", data["features"][0]), ("evidence", evidence)):
            (tmp_path / f"{day}-{suffix}.json").write_text(encode_dataset(payload))
        rows.append({"date": day, "joined": [f"{day}-joined.json"],
                     "features": [f"{day}-features.json"], "evidence": f"{day}-evidence.json"})
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"version": "historical-training-manifest-v1", "sessions": rows,
                               "watch_config": asdict(CONFIG)}))
    return path


def test_manifest_partitions_news_purge_and_missing_evidence_audit(tmp_path):
    path = manifest(tmp_path)
    groups, _, report, _ = prepare_historical(path, "2018-07-16", "2018-07-23", 86400)
    assert all(len(rows) == 1 for rows in groups.values())
    assert report["basic_coverage_passed"]
    path = manifest(tmp_path, shared=True)
    with pytest.raises(ValueError, match="empty_after_purge"):
        prepare_historical(path, "2018-07-16", "2018-07-23", 86400)
    path = manifest(tmp_path, float_known=False)
    _, _, report, _ = prepare_historical(path, "2018-07-16", "2018-07-23", 86400, require_coverage=False)
    assert not report["basic_coverage_passed"]
    assert report["partitions"]["train"][0]["entry_block_counts"]["historical_float_unknown"] == 8


def test_tampered_features_rejected():
    data, _ = session()
    data["features"][0]["states"][0]["minute_macd"] = 999
    with pytest.raises(ValueError, match="digest"):
        HistoricalLearningReplay(data)


def test_unpriced_position_fails_instead_of_learning_a_zero_reward():
    data, _ = session()
    replay = HistoricalLearningReplay(data)
    replay.step({"A": 1})
    sim = replay.engine.simulators["A"]
    for level in sim.books[sim.states[2]["candle_id"]]["bids"]:
        level["size"] = 0
    with pytest.raises(ValueError, match="Unpriced"):
        replay.step({})


def test_historical_cli_trains_resumes_and_labels_results(tmp_path, monkeypatch):
    from drl_agent.train_portfolio import main
    source = manifest(tmp_path)
    output = tmp_path / "run"
    args = ["train", "--historical", "--manifest", str(source), "--train-end", "2018-07-16",
            "--validation-end", "2018-07-23", "--output", str(output), "--slots", "2", "--threads", "1"]
    monkeypatch.setattr("sys.argv", [*args, "--epochs", "1"])
    main()
    monkeypatch.setattr("sys.argv", [*args, "--epochs", "2", "--resume"])
    main()
    progress = json.loads((output / "progress.json").read_text())
    assert progress["episodes"] == 2 and progress["transitions"] == 14
    assert progress["identity"]["replay_mode"] == "historical-synthetic"
    report = json.loads((output / "validation-performance.json").read_text())
    assert report["synthetic"] and report["retrospective"]
    assert not (output / "test-performance.json").exists()


@pytest.mark.skipif(os.getenv("DRL_STRESS_TEST") != "1", reason="Explicit historical learning stress test")
def test_thousand_historical_learning_episodes():
    torch.set_num_threads(1)
    torch.manual_seed(42)
    data, _ = session()
    encoder = PortfolioEncoder(FEATURE_NAMES, slots=2)
    agent = make_agent(encoder)
    transitions = 0
    for _ in range(1000):
        result = learn_session(HistoricalLearningReplay(data), agent, encoder)
        transitions += result["transitions"]
    assert transitions == 7000
    assert all(torch.isfinite(v).all() for v in agent.policy.state_dict().values())
    print({"fixture_only": True, "historical_adapter_episodes": 1000,
           "transitions": transitions, "broker_calls": 0})
