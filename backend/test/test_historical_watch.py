from copy import deepcopy
from dataclasses import replace
import json

import pytest

from drl_agent.historical_features import build_features
from drl_agent.historical_watch import build_watch
from drl_agent.synthetic_portfolio import SyntheticPortfolio
from drl_agent.watch_replay import WatchConfig
from test.test_synthetic_execution import fixture, resign
from test.test_synthetic_portfolio import BUY, SELL

CONFIG = WatchConfig(1, 20, 3, 20_000_000, 1, watch_window_seconds=604800)


def inputs(symbols="AB"):
    datasets, features, evidence = [], [], []
    for symbol in symbols:
        joined = fixture(symbol=symbol)
        ts = joined["states"][0]["decision_ts"]
        joined["articles"] = [{"candidate_id": symbol + "-news", "replay_available_ts": ts}]
        for s in joined["states"]:
            s["news_candidate_ids"] = [symbol + "-news"]
            s["news_watch_active"] = True
        resign(joined, "join_sha256")
        feature = build_features(joined)
        # Explicit test evidence; eight fixture bars do not establish real RVOL.
        for s in feature["states"]:
            s["rvol"], s["rvol_valid"] = (4 if symbol == "A" else 5), True
            s.update(bar_vwap_estimate=10, minute_macd=.2, minute_macd_signal=.1)
        resign(feature, "features_sha256")
        datasets.append(joined)
        features.append(feature)
        for kind, value, basis in (("price", 10.5, "nominal"), ("float", 1_000_000, "float_shares")):
            evidence.append(dict(symbol=symbol, kind=kind, value=value, basis=basis, source="test-fixture",
                                 effective_ts=ts, available_ts=ts, valid_until_ts=ts + 3600))
    return datasets, features, evidence


def test_thresholds_capacity_recheck_and_provenance():
    datasets, features, evidence = inputs()
    watch = build_watch(datasets, features, evidence, config=CONFIG)
    assert watch["frames"][0]["watching"][0]["symbol"] == "B"
    assert watch["frames"][0]["rejected"]["A"] == ["watch_capacity"]
    assert watch["frames"][0]["watching"][0]["evidence_ids"]
    features[1]["states"][2]["rvol"] = 1
    resign(features[1], "features_sha256")
    changed = build_watch(datasets, features, evidence, config=CONFIG)
    assert changed["frames"][:2] == watch["frames"][:2]
    assert changed["frames"][2]["watching"][0]["symbol"] == "A"
    assert changed["frames"][2]["rejected"]["B"] == ["watch_capacity"]


def test_watch_quiet_news_then_cross_rvol_and_evaluate_technicals_before_entry():
    datasets, features, evidence = inputs("A")
    for state, rvol in zip(features[0]["states"], [.5, 2, 3.2, 4, 4, 1, 4, 4]):
        state["rvol"] = rvol
    features[0]["states"][2]["bar_vwap_estimate"] = 11
    resign(features[0], "features_sha256")
    watch = build_watch(datasets, features, evidence, config=CONFIG)
    candidates = [frame["watching"][0] for frame in watch["frames"]]
    assert all(c["symbol"] == "A" for c in candidates)
    assert candidates[0]["stage"] == "watching"
    assert candidates[2]["rvol_crossed_threshold"]
    assert candidates[2]["rvol_delta"] == pytest.approx(1.2)
    assert candidates[2]["stage"] == "evaluating"
    assert candidates[2]["entry_block_reasons"] == ["price_not_above_vwap"]
    assert candidates[3]["research_entry_ready"]
    assert not candidates[5]["research_entry_ready"]
    sim = SyntheticPortfolio(datasets, session_date="2018-07-16", watch=watch)
    assert "A" in sim.observation()["symbols"]
    for _ in range(3):
        assert sim.step({"A": BUY})["symbols"]["A"]["reason"] == "historical_entry_not_ready"
    assert sim.step({"A": BUY})["symbols"]["A"]["fill"]["side"] == "buy"
    assert sim.step({"A": SELL})["symbols"]["A"]["fill"]["side"] == "sell"


def test_unknown_float_and_macd_do_not_hide_watch_but_block_entry():
    datasets, features, evidence = inputs("A")
    for state in features[0]["states"]:
        state["minute_macd"] = None
    resign(features[0], "features_sha256")
    watch = build_watch(datasets, features, evidence[:1], config=CONFIG)
    candidate = watch["frames"][0]["watching"][0]
    assert candidate["momentum_triggered"] and not candidate["research_entry_ready"]
    assert set(candidate["entry_block_reasons"]) == {"historical_float_unknown", "macd_unknown"}
    assert candidate["float_shares"] is None


def test_missing_data_iex_and_adjusted_price_never_qualify():
    datasets, features, evidence = inputs("A")
    result = build_watch(datasets, features, [], config=CONFIG)
    assert result["frames"][0]["rejected"]["A"] == ["historical_nominal_price_unknown"]
    for s in features[0]["states"]:
        s["volume_source"] = "iex"
    resign(features[0], "features_sha256")
    result = build_watch(datasets, features, evidence, config=CONFIG)
    assert result["frames"][0]["entry_blocked"]["A"] == ["consolidated_rvol_unavailable"]
    evidence[0]["basis"] = "adjusted"
    with pytest.raises(ValueError):
        build_watch(datasets, features, evidence, config=CONFIG)


def test_future_evidence_and_expiration_cannot_leak_or_extend_news():
    datasets, features, evidence = inputs("A")
    ts = datasets[0]["states"][0]["decision_ts"]
    evidence[1]["available_ts"] = ts + 120
    watch = build_watch(datasets, features, evidence, config=replace(CONFIG, watch_window_seconds=180))
    assert watch["frames"][1]["watching"]
    assert not watch["frames"][1]["watching"][0]["research_entry_ready"]
    assert watch["frames"][2]["watching"]
    assert "news_watch_inactive" in watch["frames"][3]["rejected"]["A"]
    evidence[1]["valid_until_ts"] = ts + 180
    expired = build_watch(datasets, features, evidence, config=CONFIG)
    assert "historical_float_unknown" in expired["frames"][3]["entry_blocked"]["A"]


def test_gate_blocks_new_buys_not_exits_and_filters_observation():
    datasets, features, evidence = inputs()
    watch = build_watch(datasets, features, evidence, config=CONFIG)
    sim = SyntheticPortfolio(datasets, session_date="2018-07-16", watch=watch)
    assert set(sim.observation()["symbols"]) == {"B"}
    event = sim.step({"A": BUY, "B": BUY})
    assert event["symbols"]["A"]["reason"] == "outside_historical_watch"
    assert event["symbols"]["B"]["fill"]["side"] == "buy"
    event = sim.step({"B": SELL})
    assert event["symbols"]["B"]["fill"]["side"] == "sell"
    assert sim.report()["watch_sha256"] == watch["watch_sha256"]


def test_loss_of_eligibility_blocks_pending_entry_and_does_not_hide_held_position():
    datasets, features, evidence = inputs("A")
    for s in features[0]["states"][2:]:
        s["rvol"] = 0
    resign(features[0], "features_sha256")
    watch = build_watch(datasets, features, evidence, config=CONFIG)
    sim = SyntheticPortfolio(datasets, session_date="2018-07-16", watch=watch)
    sim.step({"A": {**BUY, "quantity": 1}})
    event = sim.step({"A": BUY})
    assert event["symbols"]["A"]["reason"] == "historical_entry_not_ready"
    assert "A" in sim.observation()["symbols"]
    assert not sim.observation()["watch"]["watching"][0]["research_entry_ready"]
    assert sim.step({"A": SELL})["symbols"]["A"]["fill"]


def test_hashes_duplicates_and_input_isolation():
    datasets, features, evidence = inputs()
    original = deepcopy((datasets, features, evidence))
    watch = build_watch(datasets, features, evidence, config=CONFIG)
    assert (datasets, features, evidence) == original
    with pytest.raises(ValueError, match="duplicate"):
        build_watch(datasets, features, evidence + evidence[:1], config=CONFIG)
    watch["source_hashes"]["A"] = "bad"
    resign(watch, "watch_sha256")
    with pytest.raises(ValueError, match="sources"):
        SyntheticPortfolio(datasets, session_date="2018-07-16", watch=watch)


@pytest.mark.parametrize("kind,value,reason", [("price", 30, "price_out_of_range"),
                                               ("float", 30_000_000, "float_above_threshold")])
def test_expensive_and_high_float_candidates_rejected(kind, value, reason):
    datasets, features, evidence = inputs("A")
    next(e for e in evidence if e["kind"] == kind)["value"] = value
    watch = build_watch(datasets, features, evidence, config=CONFIG)
    if kind == "price":
        assert reason in watch["frames"][0]["rejected"]["A"]
        assert not watch["frames"][0]["watching"]
    else:
        assert reason in watch["frames"][0]["entry_blocked"]["A"]
        assert not watch["frames"][0]["watching"][0]["research_entry_ready"]


def test_cli_outputs_auditable_watch_and_refuses_overwrite(tmp_path, monkeypatch):
    from dataclasses import asdict
    from drl_agent.historical_watch import main
    datasets, features, evidence = inputs("A")
    for filename, data in (("joined.json", datasets[0]), ("features.json", features[0]), ("evidence.json", evidence)):
        (tmp_path / filename).write_text(json.dumps(data))
    manifest = {"joined": ["joined.json"], "features": ["features.json"],
                "evidence": "evidence.json", "config": asdict(CONFIG)}
    source, output = tmp_path / "manifest.json", tmp_path / "watch.json"
    source.write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", ["run", "--manifest", str(source), "--output", str(output)])
    main()
    watch = json.loads(output.read_text())
    assert watch["frames"][0]["watching"][0]["symbol"] == "A"
    assert watch["watch_sha256"]
    with pytest.raises(FileExistsError):
        main()


def test_adjusted_fallback_is_explicit_and_never_overrides_known_expensive_price():
    datasets, features, evidence = inputs("A")
    floats = [r for r in evidence if r["kind"] == "float"]
    strict = build_watch(datasets, features, floats, config=CONFIG)
    assert not strict["frames"][0]["watching"]
    fallback = build_watch(datasets, features, floats, config=CONFIG, allow_adjusted_price=True)
    candidate = fallback["frames"][0]["watching"][0]
    assert candidate["price_basis"] == "adjusted_research_fallback"
    assert candidate["price"] == 10.5
    assert len(candidate["evidence_ids"]) == 1
    assert fallback["adjusted_price_fallback_states"] == 8
    assert not fallback["training_ready"]
    evidence[0]["value"] = 100
    known = build_watch(datasets, features, evidence, config=CONFIG, allow_adjusted_price=True)
    assert not known["frames"][0]["watching"]
    assert "price_out_of_range" in known["frames"][0]["rejected"]["A"]
    assert known["adjusted_price_fallback_states"] == 0


def test_fallback_keeps_float_and_volume_requirements_and_tracks_float_age():
    datasets, features, evidence = inputs("A")
    watch = build_watch(datasets, features, [], config=CONFIG, allow_adjusted_price=True)
    assert "historical_float_unknown" in watch["frames"][0]["entry_blocked"]["A"]
    floats = [r for r in evidence if r["kind"] == "float"]
    floats[0]["effective_ts"] -= 86400 * 30
    watch = build_watch(datasets, features, floats, config=CONFIG, allow_adjusted_price=True)
    assert watch["frames"][0]["watching"][0]["float_age_seconds"] == 86400 * 30
    assert watch["frames"][1]["watching"][0]["float_age_seconds"] == 86400 * 30 + 60
    for state in features[0]["states"]:
        state["volume_source"] = "iex"
    resign(features[0], "features_sha256")
    watch = build_watch(datasets, features, floats, config=CONFIG, allow_adjusted_price=True)
    assert watch["frames"][0]["entry_blocked"]["A"] == ["consolidated_rvol_unavailable"]
    with pytest.raises(ValueError, match="boolean"):
        build_watch(datasets, features, floats, config=CONFIG, allow_adjusted_price="true")


def test_venue_proxy_compares_own_history_without_inflating_volume():
    datasets, features, evidence = inputs("A")
    for s in features[0]["states"]:
        s.update(volume_source="iex", rvol=None, rvol_valid=False,
                 observed_volume_rvol=4.2, observed_volume_rvol_valid=True,
                 prior_minute_coverage=.8, current_minute_coverage=.7)
    resign(features[0], "features_sha256")
    watch = build_watch(datasets, features, evidence, config=CONFIG, allow_venue_rvol=True)
    candidate = watch["frames"][0]["watching"][0]
    assert candidate["rvol"] == 4.2
    assert candidate["rvol_basis"] == "iex_only_partial_observation_proxy"
    assert candidate["prior_minute_coverage"] == .8
    assert candidate["current_minute_coverage"] == .7
    assert watch["venue_rvol_states"] == 8
    assert not watch["training_ready"]
    for s in features[0]["states"]:
        s["observed_volume_rvol_valid"] = False
    resign(features[0], "features_sha256")
    watch = build_watch(datasets, features, evidence, config=CONFIG, allow_venue_rvol=True)
    assert watch["frames"][0]["watching"]
    assert "rvol_unknown" in watch["frames"][0]["entry_blocked"]["A"]
