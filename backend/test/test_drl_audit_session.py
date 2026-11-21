import pytest
from dataclasses import replace

from drl_agent.audit_session import audit_sessions
from test.test_drl_partitions import day, sign
from test.test_drl_portfolio_replay import WATCH


def test_usable_coverage_is_not_training_approval():
    report = audit_sessions([[day(0)]], watch_config=WATCH)
    session = report["sessions"][0]
    assert session["basic_coverage_passed"]
    assert session["market_coverage_totals"]["usable_quote_states"] == 4
    assert session["fillable_entry_probes"] == 3
    assert not report["training_ready"] and not report["training_started"]
    assert report["broker_calls"] == 0


def test_present_stale_quotes_are_distinguished_from_usable_quotes():
    report = audit_sessions([[day(0, stale_at=(0, 1, 2, 3))]], watch_config=WATCH)
    session = report["sessions"][0]
    assert session["market_coverage_totals"]["present_quote_states"] == 4
    assert session["market_coverage_totals"]["usable_quote_states"] == 0
    assert session["entry_probe_rejections"] == {"entry_waiting_for_fresh_quote": 3}
    assert "no_usable_quotes" in session["coverage_blockers"]


def test_hype_failure_separate_from_market_data_coverage():
    data = day(0)
    for state in data["states"]:
        for item in state["inputs"]["scanner"]:
            item["record"]["payload"]["semantic_signal"]["hype"]["long_hype_score"] = 0.05
    session = audit_sessions([[sign(data)]], watch_config=WATCH)["sessions"][0]
    assert session["market_coverage_totals"]["usable_quote_states"] == 4
    assert session["entry_probe_rejections"] == {"hype_below_threshold": 3}
    assert session["coverage_blockers"] == ["no_fillable_watch_entries"]


def test_legacy_watch_fields_missing_market_and_bad_hash_fail_closed():
    data = day(0)
    for state in data["states"]:
        for item in state["inputs"]["scanner"]:
            item["record"]["payload"].pop("watch_inputs")
        for kind in ("levelone_quote", "level2_book", "minute_candle"):
            state["inputs"][kind] = []
    session = audit_sessions([[sign(data)]], watch_config=WATCH)["sessions"][0]
    assert session["watch_rejections"] == {"watch_inputs_missing": 4}
    assert set(session["coverage_blockers"]) == {"no_qualifying_watch_candidates", "no_usable_quotes",
                                               "no_fresh_valid_depth", "no_fresh_completed_candles", "no_fillable_watch_entries"}
    data["manifest"]["dataset_sha256"] = "bad"
    with pytest.raises(ValueError, match="digest"):
        audit_sessions([[data]], watch_config=WATCH)


def test_duplicate_dates_rejected():
    with pytest.raises(ValueError, match="strictly increase"):
        audit_sessions([[day(0)], [day(0)]], watch_config=WATCH)


def test_background_quotes_cannot_hide_missing_candidate_quotes():
    target = day(0, "A", stale_at=(0, 1, 2, 3))
    background = day(0, "B")
    for state in background["states"]:
        for item in state["inputs"]["scanner"]:
            item["record"]["payload"]["watch_inputs"]["price"] = 100
    session = audit_sessions([[target, sign(background)]], watch_config=WATCH)["sessions"][0]
    assert session["market_coverage_totals"]["usable_quote_states"] == 4
    assert session["watch_market_coverage_totals"]["usable_quote_states"] == 0
    assert session["watch_market_coverage"]["B"]["states"] == 0
    assert session["watch_market_coverage_totals"]["states"] == session["watch_symbol_ticks"] == 4
    assert "no_usable_quotes" in session["coverage_blockers"]


def test_watch_coverage_excludes_states_after_watch_expires():
    session = audit_sessions([[day(0, stale_at=(2, 3))]],
                             watch_config=replace(WATCH, watch_window_seconds=2))["sessions"][0]
    assert session["market_coverage_totals"]["states"] == 4
    assert session["watch_market_coverage_totals"]["states"] == session["watch_symbol_ticks"] == 2
    assert session["watch_market_coverage_totals"]["usable_quote_states"] == 2
