import json

import pytest

from drl_agent.candidate_session_report import report
from drl_agent.corpus_gap_audit import signed


def fixture(tmp_path, *, volume_missing=False, extra_blocker=None):
    config = {"minimum_rvol": 3}
    candidates = [{"entry_block_reasons": ["historical_float_unknown"], "momentum_triggered": True,
                   "research_entry_ready": False, "rvol": 4, "rvol_valid": True},
                  {"entry_block_reasons": ["historical_float_unknown", "rvol_below_threshold"],
                   "momentum_triggered": False, "research_entry_ready": False, "rvol": 2, "rvol_valid": True}]
    if volume_missing:
        candidates[0].update(entry_block_reasons=["historical_float_unknown", "rvol_unknown"],
                             momentum_triggered=False, rvol=None, rvol_valid=False)
    if extra_blocker:
        candidates[0]["entry_block_reasons"].append(extra_blocker)
    watch = signed({"version": "historical-watch-v2", "config": config,
                    "frames": [{"watching": candidates}]}, "watch_sha256")
    summary = signed({"version": "candidate-session-summary-v1", "symbol": "ABC", "date": "2026-08-07",
        "watch_sha256": watch["watch_sha256"], "join_sha256": "joined", "features_sha256": "features",
        "watched_states": 2, "momentum_states": 0 if volume_missing else 1, "entry_ready_states": 0,
        "coverage": {"rvol_states": 1 if volume_missing else 2,
                     "strict_rvol_unavailable": {"incomplete_prior_sessions": 1} if volume_missing else {}},
        "session_coverage": {"full_regular_price_session": False},
        "entry_block_counts": {"historical_float_unknown": 2, "rvol_below_threshold": 1,
                               **({"rvol_unknown": 1} if volume_missing else {}),
                               **({extra_blocker: 1} if extra_blocker else {})}}, "summary_sha256")
    directory = tmp_path / "ABC_2026-08-07"
    directory.mkdir()
    for name, data in (("watch", watch), ("summary", summary)):
        (directory / (name + ".json")).write_text(json.dumps(data))
    (tmp_path / "config.json").write_text(json.dumps({"watch_config": config, "plan_sha256": "plan"}))
    (tmp_path / "progress.json").write_text(json.dumps({"status": "paused_at_batch_limit", "total_sessions": 3,
        "completed": {"ABC:2026-08-07": {key: summary[key] for key in
            ("join_sha256", "features_sha256", "watch_sha256", "summary_sha256")}}}))
    return directory


def test_readiness_counts_do_not_claim_performance(tmp_path):
    fixture(tmp_path)
    result = report(tmp_path)
    assert result["remaining_sessions"] == 2 and result["unique_symbols"] == 1
    assert result["totals"]["float_only_blocked_states"] == 1
    assert result["totals"]["watched_states"] == 2
    assert result["totals"]["entry_ready_states"] == 0
    assert result["totals"]["volume_evidence_needed_states"] == 0
    assert result["by_symbol"][0]["maximum_watched_rvol"] == 4
    momentum = result["momentum_sessions"][0]
    assert (momentum["symbol"], momentum["date"], momentum["states"], momentum["maximum_rvol"]) == (
        "ABC", "2026-08-07", 1, 4)
    assert momentum["entry_block_counts"] == {"historical_float_unknown": 1}
    assert momentum["float_only_blocked_states"] == 1 and momentum["entry_ready_states"] == 0
    assert not result["training_started"] and result["network_calls"] == 0


def test_changed_summary_is_rejected(tmp_path):
    directory = fixture(tmp_path)
    path = directory / "summary.json"
    data = json.loads(path.read_text())
    data["entry_ready_states"] = 2
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        report(tmp_path)


def test_changed_config_is_rejected(tmp_path):
    fixture(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"watch_config": {"minimum_rvol": 2}, "plan_sha256": "plan"}))
    with pytest.raises(ValueError, match="provenance"):
        report(tmp_path)


def test_volume_repair_leads_do_not_become_eligible_or_momentum(tmp_path):
    fixture(tmp_path, volume_missing=True)
    result = report(tmp_path)
    assert result["totals"]["volume_evidence_needed_states"] == 1
    assert result["totals"]["volume_evidence_needed_sessions"] == 1
    assert result["totals"]["momentum_states"] == result["totals"]["entry_ready_states"] == 0
    assert result["totals"]["float_only_blocked_states"] == 0
    assert result["volume_repair_sessions"][0]["symbol"] == "ABC"
    assert result["all_state_rvol_unavailable_counts"] == {"incomplete_prior_sessions": 1}
    assert result["momentum_sessions"] == []


def test_momentum_diagnostics_keep_other_entry_blockers(tmp_path):
    fixture(tmp_path, extra_blocker="macd_not_bullish")
    row = report(tmp_path)["momentum_sessions"][0]
    assert row["states"] == 1 and row["float_only_blocked_states"] == 0
    assert row["entry_block_counts"] == {"historical_float_unknown": 1, "macd_not_bullish": 1}


@pytest.mark.parametrize("blocker", ["vwap_unknown", "macd_not_bullish", "outside_regular_session"])
def test_other_technical_blockers_exclude_volume_only_repair_leads(tmp_path, blocker):
    fixture(tmp_path, volume_missing=True, extra_blocker=blocker)
    result = report(tmp_path)
    assert result["totals"]["volume_evidence_needed_states"] == 0
    assert result["volume_repair_sessions"] == []


def test_assembly_cli_writes_readiness(tmp_path, monkeypatch, capsys):
    from dataclasses import asdict
    import sys
    from drl_agent.candidate_sessions import main
    from test.test_candidate_sessions import inputs
    plan, receipts, config = inputs(tmp_path)
    for name, value in (("plan", plan), ("receipts", receipts), ("watch_config", {"watch_config": asdict(config)})):
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    argv = ["candidate_sessions"]
    for name, value in {"plan": tmp_path / "plan.json", "corpus": tmp_path, "audit-dir": tmp_path,
        "prices": tmp_path, "trades": tmp_path, "verification": tmp_path / "receipts.json",
        "output": tmp_path / "output", "config": tmp_path / "watch_config.json", "max-sessions": 1}.items():
        argv.extend(["--" + name, str(value)])
    monkeypatch.setattr(sys, "argv", argv)
    main()
    result = json.loads((tmp_path / "output/readiness.json").read_text())
    assert result["totals"]["sessions"] == 1 and result["remaining_sessions"] == 1
    assert not result["training_started"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "paused_at_batch_limit"
