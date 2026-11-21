import json

import pytest

from drl_agent.candidate_sessions import assemble
from drl_agent.corpus_gap_audit import signed
from drl_agent.historical_news import build_universe
from drl_agent.watch_replay import WatchConfig
from test.test_alpaca_replay_export import archive


def inputs(tmp_path):
    raw = archive()
    shard = build_universe([], start_ts=1790000000, end_ts=1790300000, enrichment_cutoff_ts=1790400000)
    report = signed({"version": "historical-symbol-gap-audit-v1", "symbol": "ABC",
        "source_archives": [{"file": "raw.json", "archive_sha256": raw["archive_sha256"]}]}, "audit_sha256")
    plan = signed({"version": "candidate-gap-plan-v1", "minimum_price": 1, "maximum_price": 20,
        "watch_window_seconds": 604800, "source_news_shards": [shard["manifest_sha256"]], "candidates": [
        {"symbol": "ABC", "source_audit_sha256": report["audit_sha256"], "uncached_prior_minutes": [],
         "unsupported_prior_minutes": [], "verified_prior_minutes": [],
         "sessions": [{"date": "2026-09-21", "open_ts": 1790000000, "close_ts": 1790023400},
                      {"date": "2026-09-22", "open_ts": 1790086400, "close_ts": 1790109800}]}]}, "plan_sha256")
    progress = {"status": "complete", "completed": [{"file": "news.json", "manifest_sha256": shard["manifest_sha256"]}]}
    for name, value in (("raw", raw), ("news", shard), ("ABC", report), ("progress", progress)):
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    return plan, {"plan_sha256": plan["plan_sha256"], "completed": {}}, WatchConfig(1, 20, 3, 20000000, 50,
                                                                                              watch_window_seconds=604800)


def test_batch_newest_first_resumes_and_checks_outputs(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "output", config)
    first = assemble(*args, max_sessions=1)
    assert list(first["completed"]) == ["ABC:2026-09-22"]
    assert first["remaining"] == 1
    summary = json.loads((tmp_path / "output/ABC_2026-09-22/summary.json").read_text())
    assert summary["network_calls"] == 0 and not summary["training_started"]
    assert summary["entry_ready_states"] == 0 and not summary["float_evidence_supplied"]
    second = assemble(*args, max_sessions=1)
    assert second["status"] == "complete" and len(second["completed"]) == 2
    path = tmp_path / "output/ABC_2026-09-22/features.json"
    original = path.read_text()
    data = json.loads(original)
    data["states"][0]["rvol"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        assemble(*args)
    failed = json.loads((tmp_path / "output/progress.json").read_text())
    assert failed["status"] == "failed" and failed["error_type"] == "ValueError"
    assert len(failed["completed"]) == 2
    path.write_text(original)
    resumed = assemble(*args)
    assert resumed["status"] == "complete" and "error_type" not in resumed
    assert resumed["remaining"] == 0 and len(resumed["completed"]) == 2


def test_frozen_news_and_price_sources_checked(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "output", config)
    path = tmp_path / "raw.json"
    raw = json.loads(path.read_text())
    raw["responses"][0]["bars"]["ABC"][0]["v"] += 1
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="digest"):
        assemble(*args)
    (tmp_path / "raw.json").write_text(json.dumps(archive()))
    plan["source_news_shards"] = []
    plan = signed({k: v for k, v in plan.items() if k != "plan_sha256"}, "plan_sha256")
    receipts["plan_sha256"] = plan["plan_sha256"]
    with pytest.raises(ValueError, match="corpus differs"):
        assemble(plan, *args[1:])


def test_resume_rejects_changed_watch_settings(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "output")
    assemble(*args, config, max_sessions=1)
    changed = WatchConfig(1, 20, 4, 20000000, 50, watch_window_seconds=604800)
    with pytest.raises(ValueError, match="Frozen assembly inputs changed"):
        assemble(*args, changed, max_sessions=1)


def test_partial_requested_session_not_confused_with_missing_bars():
    from drl_agent.candidate_sessions import session_coverage
    session = {"open_ts": 1781703000, "close_ts": 1781703180}
    raw = {"request": {"start": "2026-06-17T13:30:00Z", "end": "2026-06-17T13:31:59Z"}}
    result = session_coverage(session, [raw], {"candles": [{"start_ts": 1781703000}]})
    assert result == {"expected_regular_minutes": 3, "requested_regular_minutes": 2,
        "observed_regular_minutes": 1, "unrequested_regular_minutes": 1,
        "requested_without_price_bar": 1, "full_regular_price_session": False}


def test_symbol_scope_retains_all_dates_and_is_frozen(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    plan["candidates"].append({**plan["candidates"][0], "symbol": "DEF"})
    plan = signed({k: v for k, v in plan.items() if k != "plan_sha256"}, "plan_sha256")
    receipts["plan_sha256"] = plan["plan_sha256"]
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "output", config)
    result = assemble(*args, symbols=["ABC"], max_sessions=10)
    assert result["status"] == "complete" and result["total_sessions"] == 2
    assert set(result["completed"]) == {"ABC:2026-09-21", "ABC:2026-09-22"}
    assert assemble(*args, symbols=["ABC"])["completed"] == result["completed"]
    from drl_agent.candidate_session_report import report
    assert report(tmp_path / "output")["symbol_scope"] == ["ABC"]
    for scope in (None, ["DEF"], ["ABC", "DEF"]):
        with pytest.raises(ValueError, match="Frozen assembly inputs changed"):
            assemble(*args, symbols=scope)


@pytest.mark.parametrize("scope", [[], ["UNKNOWN"], ["ABC", "ABC"], "ABC", [None]])
def test_invalid_symbol_scope_rejected(tmp_path, scope):
    plan, receipts, config = inputs(tmp_path)
    with pytest.raises(ValueError, match="symbol scope"):
        assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "output", config,
                 symbols=scope)


def earlier_archive():
    from datetime import datetime, timedelta
    from test.test_synthetic_execution import resign
    value = archive()
    def earlier(stamp):
        return (datetime.fromisoformat(stamp.replace("Z", "+00:00")) - timedelta(days=7)).isoformat()
    for field in ("start", "end"):
        value["request"][field] = earlier(value["request"][field])
    for page in value["responses"]:
        for bar in page["bars"]["ABC"]:
            bar["t"] = earlier(bar["t"])
    resign(value, "archive_sha256")
    return value


@pytest.mark.parametrize("start,bridge_end,missing", [
    ("2026-06-15T14:16:00Z", None, 46),
    ("2026-06-15T14:16:00Z", "2026-06-15T14:14:59Z", 1),
    ("2026-06-15T14:16:00Z", "2026-06-15T14:15:59Z", 0),
    ("2026-06-15T21:00:00Z", None, 390),
    ("2026-06-15T21:00:00Z", "2026-06-15T19:59:59Z", 0),
    ("2026-06-15T13:30:00Z", None, 0),
    ("2026-06-13T14:16:00Z", None, 0),
    ("2026-06-19T14:16:00Z", None, 0),
])
def test_history_extension_requires_exposed_boundary_coverage(start, bridge_end, missing):
    from drl_agent.alpaca_data_cache import _time
    from drl_agent.candidate_sessions import validate_history_boundary
    def ts(value):
        return int(_time(value).timestamp())
    extensions = [{"start_ts": ts("2026-06-12T13:30:00Z"), "end_ts": ts("2026-06-12T19:59:59Z")}]
    if bridge_end:
        extensions.append({"start_ts": ts("2026-06-15T13:30:00Z"), "end_ts": ts(bridge_end)})
    if missing:
        with pytest.raises(ValueError, match=f"exposes {missing} unrequested boundary minutes"):
            validate_history_boundary(ts(start), extensions)
    else:
        validate_history_boundary(ts(start), extensions)
    validate_history_boundary(ts(start), [])


def float_record(**changes):
    return {"symbol": "ABC", "kind": "float", "value": 1_000_000, "basis": "float_shares",
            "source": "test-fixture-not-real", "effective_ts": 1790000000,
            "available_ts": 1790000000, "valid_until_ts": 1800000000, **changes}


@pytest.mark.parametrize("stamp,accepted", [
    ("2026-06-12T18:38:00Z", True),
    ("2026-06-15T15:43:00Z", True),
    ("2026-06-15T19:59:00Z", True),
    ("2026-06-15T20:00:00Z", False),
    ("2026-06-16T15:43:00Z", False),
])
def test_history_volume_scope_includes_only_exposed_boundary_session(stamp, accepted):
    from drl_agent.alpaca_data_cache import _time
    from drl_agent.candidate_sessions import validate_history_volume_scope
    def ts(value):
        return int(_time(value).timestamp())
    extensions = [{"start_ts": ts("2026-06-12T13:30:00Z"), "end_ts": ts("2026-06-12T19:59:59Z")}]
    row = {"start_ts": ts(stamp), "available_ts": ts(stamp) + 60}
    args = (ts("2026-06-15T14:16:00Z"), extensions, [row])
    if accepted:
        validate_history_volume_scope(*args)
    else:
        with pytest.raises(ValueError, match="outside history extension"):
            validate_history_volume_scope(*args)
    with pytest.raises(ValueError, match="outside history extension"):
        validate_history_volume_scope(args[0], [], [row])


def history_gap():
    from drl_agent.alpaca_data_cache import _time
    from test.test_trade_volume_evidence import evidence
    from test.test_synthetic_execution import resign
    older = earlier_archive()
    bar = older["responses"][0]["bars"]["ABC"].pop(0)
    older["summary"]["rows"] -= 1
    resign(older, "archive_sha256")
    return older, evidence(int(_time(bar["t"]).timestamp()))


def test_history_trade_evidence_is_frozen_and_indexed(tmp_path):
    from drl_agent.candidate_dataset_index import build_index
    plan, receipts, config = inputs(tmp_path)
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts)
    root = tmp_path / "base"
    assemble(*args, root, config, max_sessions=1)
    older, trade = history_gap()
    assemble(*args, root / "history", config, max_sessions=1, history_archives=[older], history_trades=[trade])
    result = build_index(root, plan, ["history"])
    assert all(r["history_volume_hashes"] for r in result["sessions"].values())
    assert not result["training_ready"]
    with pytest.raises(ValueError, match="Frozen assembly"):
        assemble(*args, root / "history", config, history_archives=[older])
    assemble(*args, root / "removed", config, max_sessions=1, history_archives=[older])
    with pytest.raises(ValueError, match="History volume evidence removed"):
        build_index(root, plan, ["history", "removed"])
    path = root / "history/config.json"
    settings = json.loads(path.read_text())
    settings["history_volume_evidence"][0]["volume"] += 1
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="History volume evidence lineage"):
        build_index(root, plan, ["history"])


@pytest.mark.parametrize("bad", ["outside", "duplicate", "corrupt", "existing_bar"])
def test_invalid_history_trade_evidence_rejected(tmp_path, bad):
    plan, receipts, config = inputs(tmp_path)
    older, trade = history_gap()
    trades = [trade]
    if bad == "outside":
        archives, match = [], "outside history"
    else:
        archives = [older]
        if bad == "duplicate":
            trades.append(trade)
            match = "Duplicate history"
        elif bad == "corrupt":
            trade["responses"][0]["trades"][0]["s"] += 1
            match = "digest"
        else:
            archives = [earlier_archive()]
            match = "absent SIP"
    with pytest.raises(ValueError, match=match):
        assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "output", config,
                 history_archives=archives, history_trades=trades)


def test_float_input_frozen_and_original_artifacts_unchanged(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts)
    base = assemble(*args, tmp_path / "base", config, max_sessions=1)
    evidence = [float_record()]
    updated = assemble(*args, tmp_path / "floats", config, max_sessions=1, float_evidence=evidence)
    old, new = base["completed"]["ABC:2026-09-22"], updated["completed"]["ABC:2026-09-22"]
    assert old["join_sha256"] == new["join_sha256"]
    assert old["features_sha256"] == new["features_sha256"]
    assert old["watch_sha256"] != new["watch_sha256"]
    summary = json.loads((tmp_path / "floats/ABC_2026-09-22/summary.json").read_text())
    watch = json.loads((tmp_path / "floats/ABC_2026-09-22/watch.json").read_text())
    assert summary["float_evidence_supplied"]
    assert summary["float_evidence_sha256"] == watch["evidence_sha256"]
    for changed in ([], [float_record(value=2_000_000)]):
        with pytest.raises(ValueError, match="Frozen assembly"):
            assemble(*args, tmp_path / "floats", config, float_evidence=changed)
    assert assemble(*args, tmp_path / "floats", config, float_evidence=evidence)["status"] == "complete"


@pytest.mark.parametrize("evidence", [[float_record(kind="price", basis="nominal")],
    [float_record(symbol="DEF")], [float_record(value=True)], [float_record(source="")],
    [float_record(valid_until_ts=1790000000)], [float_record(), float_record()],
    [float_record(evidence_id="precomputed")], [float_record(basis="shares_outstanding")]])
def test_bad_float_input_rejected_before_writing(tmp_path, evidence):
    plan, receipts, config = inputs(tmp_path)
    with pytest.raises(ValueError):
        assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts,
                 tmp_path / "output", config, float_evidence=evidence)
    assert not (tmp_path / "output").exists()


def test_signed_sec_float_loader_checks_digest(tmp_path):
    from drl_agent.candidate_sessions import load_float_evidence
    path = tmp_path / "evidence.json"
    value = signed({"version": "sec-float-evidence-v1", "evidence": [float_record(
        basis="sec_reported_nonaffiliate_proxy")]}, "evidence_sha256")
    path.write_text(json.dumps(value))
    assert load_float_evidence(path) == value["evidence"]
    value["evidence"][0]["value"] += 1
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest"):
        load_float_evidence(path)


@pytest.mark.parametrize("basis,value,known_blocker", [
    ("float_shares", 1_000_000, None),
    ("float_shares", 30_000_000, "float_above_threshold"),
    ("sec_reported_nonaffiliate_proxy", 1_000_000, "sec_float_proxy_not_enabled")])
def test_assembly_float_is_point_in_time_and_strict(tmp_path, monkeypatch, basis, value, known_blocker):
    from test.test_historical_watch import inputs as watch_inputs
    from test.test_synthetic_execution import resign
    plan, receipts, config = inputs(tmp_path)
    datasets, features, _ = watch_inputs(["ABC"])
    joined, feature = datasets[0], features[0]
    joined["news_active_states"] = len(joined["states"])
    resign(joined, "join_sha256")
    feature["source_join_sha256"] = joined["join_sha256"]
    for row in feature["states"]:
        row.update(volume_source="alpaca_sip", price_basis="nominal", historical_nominal_price=10.5,
                   regular_session=True)
    resign(feature, "features_sha256")
    monkeypatch.setattr("drl_agent.candidate_sessions.build_session", lambda *a, **kw: (joined, feature))
    now = joined["states"][0]["decision_ts"]
    evidence = [float_record(effective_ts=now, available_ts=now + 60, valid_until_ts=now + 120,
                             basis=basis, value=value)]
    assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "out", config,
             max_sessions=1, float_evidence=evidence)
    watch = json.loads((tmp_path / "out/ABC_2026-09-22/watch.json").read_text())
    candidates = [f["watching"][0] for f in watch["frames"][:3]]
    assert candidates[0]["float_shares"] is None and candidates[2]["float_shares"] is None
    assert "historical_float_unknown" in candidates[0]["entry_block_reasons"]
    assert "historical_float_unknown" in candidates[2]["entry_block_reasons"]
    assert candidates[1]["float_shares"] == value
    assert candidates[1]["research_entry_ready"] == (known_blocker is None)
    if known_blocker:
        assert known_blocker in candidates[1]["entry_block_reasons"]


def test_session_scope_is_exact_frozen_and_order_independent(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts, tmp_path / "selected", config)
    scope = ["ABC:2026-09-21"]
    result = assemble(*args, sessions=scope)
    assert result["status"] == "complete" and list(result["completed"]) == scope
    assert not (tmp_path / "selected/ABC_2026-09-22").exists()
    from drl_agent.candidate_session_report import report
    assert report(tmp_path / "selected")["session_scope"] == scope
    assert assemble(*args, sessions=scope)["completed"] == result["completed"]
    for changed in (None, ["ABC:2026-09-22"]):
        with pytest.raises(ValueError, match="Frozen assembly"):
            assemble(*args, sessions=changed)
    other = (*args[:6], tmp_path / "ordered", config)
    both = ["ABC:2026-09-21", "ABC:2026-09-22"]
    first = assemble(*other, sessions=both, max_sessions=1)
    assert list(first["completed"]) == [both[1]]
    assert assemble(*other, sessions=list(reversed(both)))["status"] == "complete"


@pytest.mark.parametrize("scope", [[], "ABC:2026-09-21", [None],
    ["ABC:2026-09-21", "ABC:2026-09-21"], ["ABC:2026-09-23"], ["DEF:2026-09-21"]])
def test_invalid_session_scope_rejected(tmp_path, scope):
    plan, receipts, config = inputs(tmp_path)
    with pytest.raises(ValueError, match="session scope"):
        assemble(plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts,
                 tmp_path / "output", config, sessions=scope)


def test_history_extension_is_earlier_verified_and_frozen(tmp_path):
    plan, receipts, config = inputs(tmp_path)
    args = (plan, tmp_path, tmp_path, tmp_path, tmp_path, receipts)
    older = earlier_archive()
    result = assemble(*args, tmp_path / "extended", config, max_sessions=1, history_archives=[older])
    assert result["status"] == "paused_at_batch_limit"
    summary = json.loads((tmp_path / "extended/ABC_2026-09-22/summary.json").read_text())
    assert summary["history_extension_hashes"] == [older["archive_sha256"]]
    assert summary["original_archive_hashes"] == [archive()["archive_sha256"]]
    with pytest.raises(ValueError, match="Frozen assembly"):
        assemble(*args, tmp_path / "extended", config)
    with pytest.raises(ValueError, match="end before"):
        assemble(*args, tmp_path / "overlap", config, history_archives=[archive()])
    older["responses"][0]["bars"]["ABC"][0]["v"] += 1
    with pytest.raises(ValueError, match="digest"):
        assemble(*args, tmp_path / "corrupt", config, history_archives=[older])
