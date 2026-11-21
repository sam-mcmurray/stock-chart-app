from unittest.mock import MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from services import live_research_experiment as live


REGISTERED_AT = 1_789_756_100


@pytest.mark.parametrize("benchmark", [False, True])
def test_candle_reads_are_bounded_and_errors_are_not_empty_evidence(monkeypatch, benchmark):
    monkeypatch.setattr(live, "ensure_schema", MagicMock())
    context = MagicMock()
    conn = context.__enter__.return_value
    context.__exit__.return_value = False
    connect = MagicMock(return_value=context)
    monkeypatch.setattr(live.psycopg, "connect", connect)
    loader = live.load_live_benchmark_candles if benchmark else live.load_live_candles
    candidate = {"symbol": "ABC", "signal_ts": 100, "received_ts": 110}
    conn.execute.return_value.fetchall.return_value = [{"event_ts": 120}]
    assert loader(candidate) == [{"event_ts": 120}]
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert connect.call_args.kwargs["options"] == (
        "-c statement_timeout=30000 -c lock_timeout=2000"
    )
    conn.execute.side_effect = live.psycopg.errors.QueryCanceled("read timed out")
    with pytest.raises(live.psycopg.errors.QueryCanceled):
        loader(candidate)


def _snapshot(*, evidence=True, ts=REGISTERED_AT + 10):
    return {
        "id": "feature-1",
        "source": "scanner_shadow",
        "symbol": "NVDA",
        "ts": ts,
        "payload": {
            "symbol": "NVDA",
            "ts": ts,
            "semantic_signal": {"hype": {
                "feature_version": "news-hype-v1",
                "attention_score": 0.7,
                "long_hype_score": 0.0,
                "evidence_article_ids": ["article-2", "article-1"] if evidence else [],
            }},
        },
    }


def test_live_candidate_requires_scanner_provenance_boundary_and_evidence():
    assert live.record_scanner_candidate(
        {**_snapshot(), "source": "historical_news_hype"},
        received_ts=REGISTERED_AT + 20,
    )["reason"] == "not_live_scanner_snapshot"
    assert live.record_scanner_candidate(
        _snapshot(ts=REGISTERED_AT - 1), received_ts=REGISTERED_AT + 20,
    )["reason"] == "before_registration_boundary"
    assert live.record_scanner_candidate(
        _snapshot(evidence=False), received_ts=REGISTERED_AT + 20,
    )["reason"] == "no_verified_article_evidence"


def test_live_candidate_freezes_first_seen_research_only_payload(monkeypatch):
    monkeypatch.setattr(live, "ensure_schema", MagicMock())
    row = {"candidate_id": "candidate-1", "inserted": True, "sightings": 1}
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    execute = MagicMock(return_value=cursor)
    conn = MagicMock()
    conn.execute = execute
    context = MagicMock()
    context.__enter__.return_value = conn
    context.__exit__.return_value = False
    monkeypatch.setattr(live.psycopg, "connect", MagicMock(return_value=context))

    result = live.record_scanner_candidate(
        _snapshot(), received_ts=REGISTERED_AT + 20
    )

    assert result == {
        "recorded": True, "candidate_id": "candidate-1",
        "inserted": True, "sightings": 1,
    }
    params = execute.call_args.args[1]
    assert params["priority_tier"] == 0
    assert params["ranking_score"] == 0.7
    assert params["evidence_ids"].obj == ["article-1", "article-2"]
    assert params["payload"].obj["live_received"] is True
    assert params["payload"].obj["retrospective"] is False
    assert params["payload"].obj["entry_eligible"] is False
    assert params["payload"].obj["live_activation_eligible"] is False


def test_status_separates_post_scheduler_collection_health(monkeypatch):
    monkeypatch.setattr(live, "ensure_schema", MagicMock())
    row = {
        "candidate_count": 20, "cohort_count": 10, "symbol_count": 12,
        "sightings": 25, "cohort_outcome_count": 2, "complete_outcome_count": 4,
        "unevaluated_count": 0, "pending_outcome_count": 3,
        "partial_outcome_count": 2, "insufficient_outcome_count": 11,
        "first_signal_ts": REGISTERED_AT + 1, "latest_signal_ts": REGISTERED_AT + 100,
        "scheduler_candidate_count": 6, "scheduler_complete_count": 2,
        "scheduler_collecting_count": 3, "scheduler_insufficient_count": 1,
        "scheduler_late_candle_count": 1, "scheduler_no_candle_count": 0,
    }
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    execute = MagicMock(return_value=cursor)
    conn = MagicMock()
    conn.execute = execute
    context = MagicMock()
    context.__enter__.return_value = conn
    context.__exit__.return_value = False
    monkeypatch.setattr(live.psycopg, "connect", MagicMock(return_value=context))

    result = live.status()

    assert result["collection_scheduler"] == {
        "version": "live-stream-priority-v2",
        "started_at": live.LIVE_COLLECTION_SCHEDULER_STARTED_AT,
        "candidate_count": 6,
        "complete_count": 2,
        "collecting_count": 3,
        "insufficient_count": 1,
        "late_candle_count": 1,
        "no_candle_count": 0,
    }
    assert execute.call_args.args[1]["scheduler_started_at"] == live.LIVE_COLLECTION_SCHEDULER_STARTED_AT


def test_live_outcome_uses_only_later_regular_stream_candles():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 9, 18, 10, 0, tzinfo=eastern).timestamp())
    candidate = {
        "candidate_id": "candidate-1", "symbol": "NVDA",
        "signal_ts": signal_ts, "received_ts": signal_ts,
    }
    candles = [{
        "observation_key": f"candle-{index}",
        "event_ts": signal_ts + 60 * (index + 1),
        "received_ts": signal_ts + 60 * (index + 1),
        "open": 100.0, "high": 101.0 + index / 100,
        "low": 99.0, "close": 100.5, "volume": 1000,
    } for index in range(60)]
    candles.insert(0, {
        "event_ts": signal_ts - 60, "received_ts": signal_ts,
        "open": 1, "high": 500, "low": 1, "close": 500, "volume": 1,
    })

    outcome = live.evaluate_live_outcome(candidate, candles)

    assert outcome["status"] == "complete"
    assert outcome["candle_count"] == 60
    assert set(outcome["horizons"]) == {"5", "15", "60"}
    assert outcome["horizons"]["60"]["return_pct"] == pytest.approx(0.5)
    assert outcome["retrospective"] is False
    assert outcome["live_received"] is True
    assert outcome["live_activation_eligible"] is False


def test_live_outcome_attaches_only_entry_aligned_live_benchmark():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 9, 18, 10, 0, tzinfo=eastern).timestamp())
    candidate = {
        "candidate_id": "candidate-1", "symbol": "NVDA",
        "signal_ts": signal_ts, "received_ts": signal_ts,
    }
    candles = [{
        "event_ts": signal_ts + 60 * (index + 1),
        "received_ts": signal_ts + 60 * (index + 1),
        "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 100,
    } for index in range(60)]
    benchmark = [{
        "event_ts": signal_ts + 60 * (index + 1),
        "received_ts": signal_ts + 60 * (index + 1),
        "open": 100, "high": 101, "low": 99, "close": 101, "volume": 100,
    } for index in range(60)]

    aligned = live.evaluate_live_outcome(
        candidate, candles, benchmark_candles=benchmark,
    )
    delayed = live.evaluate_live_outcome(
        candidate, candles, benchmark_candles=benchmark[1:],
    )

    assert aligned["horizons"]["60"]["benchmark_symbol"] == "SPY"
    assert aligned["horizons"]["60"]["benchmark_return_pct"] == pytest.approx(1)
    assert aligned["horizons"]["60"]["excess_return_pct"] == pytest.approx(-0.5)
    assert "benchmark_return_pct" not in delayed["horizons"]["60"]


def test_live_outcome_stays_partial_until_primary_horizon():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 9, 18, 10, 0, tzinfo=eastern).timestamp())
    candidate = {
        "candidate_id": "candidate-1", "symbol": "NVDA",
        "signal_ts": signal_ts, "received_ts": signal_ts,
    }
    candles = [{
        "event_ts": signal_ts + 60 * (index + 1),
        "received_ts": signal_ts + 60 * (index + 1),
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100,
    } for index in range(15)]

    outcome = live.evaluate_live_outcome(candidate, candles)

    assert outcome["status"] == "partial"
    assert set(outcome["horizons"]) == {"5", "15"}


def test_live_outcome_rejects_a_late_first_regular_session_candle():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 9, 18, 10, 0, tzinfo=eastern).timestamp())
    candidate = {
        "candidate_id": "candidate-1", "symbol": "NVDA",
        "signal_ts": signal_ts, "received_ts": signal_ts,
    }
    candles = [{
        "event_ts": signal_ts + 15 * 60,
        "received_ts": signal_ts + 15 * 60,
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100,
    }]

    outcome = live.evaluate_live_outcome(candidate, candles)

    assert outcome["status"] == "insufficient"
    assert outcome["reason"] == "missed_live_entry_window"
    assert outcome["entry_timing_valid"] is False
    assert outcome["horizons"] == {}


def test_live_outcome_waits_for_candle_while_entry_window_is_open():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 9, 18, 10, 0, tzinfo=eastern).timestamp())
    candidate = {
        "candidate_id": "candidate-1", "symbol": "NVDA",
        "signal_ts": signal_ts, "received_ts": signal_ts,
    }

    outcome = live.evaluate_live_outcome(
        candidate, [], now_ts=signal_ts + live.LIVE_ENTRY_GRACE_SECONDS,
    )

    assert outcome["status"] == "pending"


def test_live_outcome_expires_without_spending_a_stream_slot_on_a_late_candle():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 9, 18, 10, 0, tzinfo=eastern).timestamp())
    candidate = {
        "candidate_id": "candidate-1", "symbol": "NVDA",
        "signal_ts": signal_ts, "received_ts": signal_ts,
    }

    outcome = live.evaluate_live_outcome(
        candidate, [], now_ts=signal_ts + live.LIVE_ENTRY_GRACE_SECONDS + 1,
    )

    assert outcome["status"] == "insufficient"
    assert outcome["reason"] == "entry_window_elapsed_without_candle"
    assert outcome["entry_timing_valid"] is False
    assert outcome["entry_event_ts"] is None


def test_live_outcome_accepts_monday_open_after_friday_close_signal():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 9, 18, 16, 30, tzinfo=eastern).timestamp())
    entry_ts = int(datetime(2026, 9, 21, 9, 31, tzinfo=eastern).timestamp())
    candidate = {
        "candidate_id": "candidate-1", "symbol": "NVDA",
        "signal_ts": signal_ts, "received_ts": signal_ts,
    }
    candles = [{
        "event_ts": entry_ts,
        "received_ts": entry_ts,
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100,
    }]

    outcome = live.evaluate_live_outcome(candidate, candles)

    assert outcome["status"] == "partial"
    assert outcome["entry_timing"] == "next_regular_open"
    assert outcome["entry_timing_valid"] is True


def test_entry_window_skips_nyse_good_friday_holiday():
    eastern = ZoneInfo("America/New_York")
    signal_ts = int(datetime(2026, 4, 2, 16, 30, tzinfo=eastern).timestamp())
    expected_open = int(datetime(2026, 4, 6, 9, 30, tzinfo=eastern).timestamp())

    start, end, timing = live._entry_window(signal_ts)

    assert timing == "next_regular_open"
    assert start == expected_open
    assert end == expected_open + live.LIVE_OPEN_GRACE_SECONDS


def test_entry_window_respects_nyse_early_close():
    eastern = ZoneInfo("America/New_York")
    after_early_close = int(datetime(2026, 11, 27, 13, 5, tzinfo=eastern).timestamp())
    expected_open = int(datetime(2026, 11, 30, 9, 30, tzinfo=eastern).timestamp())

    start, _, timing = live._entry_window(after_early_close)

    assert timing == "next_regular_open"
    assert start == expected_open
    assert live._regular_minute(
        int(datetime(2026, 11, 27, 12, 59, tzinfo=eastern).timestamp())
    ) is True
    assert live._regular_minute(after_early_close) is False


def _report_row(index, *, priority_tier=0, entry_timing_valid=True):
    return {
        "candidate_id": f"candidate-{index:04d}",
        "signal_ts": REGISTERED_AT + index,
        "priority_tier": priority_tier,
        "ranking_score": index / 10,
        "outcome_payload": {
            "entry_timing_valid": entry_timing_valid,
            "horizons": {
                "60": {
                    "mfe_pct": float(index),
                    "return_pct": float(index - 4),
                    "mae_pct": float(-index),
                }
            },
        },
    }


def test_live_report_uses_only_timing_valid_locked_cohort():
    rows = [_report_row(index) for index in range(1, 9)]
    rows.append(_report_row(9, priority_tier=1))
    rows.append(_report_row(10, entry_timing_valid=False))

    report = live.build_live_report(rows)

    assert report["source_complete_count"] == 10
    assert report["usable_timing_valid_count"] == 9
    assert report["hypothesis_cohort"]["count"] == 8
    assert report["hypothesis_cohort"]["mean_mfe_pct"] == pytest.approx(4.5)
    assert report["hypothesis_cohort"]["mean_return_pct"] == pytest.approx(0.5)
    assert report["hypothesis_cohort"]["mean_mae_pct"] == pytest.approx(-4.5)
    assert report["hypothesis_cohort"]["win_rate_pct"] == pytest.approx(50)
    assert report["top_attention_quartile"]["count"] == 2
    assert report["top_attention_quartile"]["mean_mfe_pct"] == pytest.approx(7.5)
    assert report["top_vs_cohort_mean_mfe_difference_pct"] == pytest.approx(3.0)
    assert report["formal_readout_available"] is False
    assert report["readout_status"] == "accumulating"
    assert report["activation_eligible"] is False


def test_live_report_summarizes_only_available_live_benchmark_coverage():
    benchmarked = _report_row(5)
    benchmarked["outcome_payload"]["horizons"]["60"]["benchmark_return_pct"] = 0.25

    report = live.build_live_report([benchmarked, _report_row(6)])

    metrics = report["hypothesis_cohort"]
    assert report["benchmark_symbol"] == "SPY"
    assert metrics["benchmark_coverage_count"] == 1
    assert metrics["mean_benchmark_return_pct"] == pytest.approx(0.25)
    assert metrics["mean_excess_return_pct"] == pytest.approx(0.75)


def test_live_report_becomes_formally_readable_at_registered_sample_boundary():
    rows = [_report_row(index) for index in range(1, 501)]

    report = live.build_live_report(rows)

    assert report["hypothesis_cohort"]["count"] == 500
    assert report["minimum_live_received_sample"] == 500
    assert report["sample_progress_pct"] == 100
    assert report["formal_readout_available"] is True
    assert report["readout_status"] == "sample_ready"
    assert report["activation_eligible"] is False


def test_unchanged_live_outcome_advances_fair_poll_timestamp(monkeypatch):
    monkeypatch.setattr(live, "ensure_schema", MagicMock())
    execute = MagicMock()
    conn = MagicMock()
    conn.execute = execute
    context = MagicMock()
    context.__enter__.return_value = conn
    context.__exit__.return_value = False
    monkeypatch.setattr(live.psycopg, "connect", MagicMock(return_value=context))
    outcome = {
        "candidate_id": "candidate-1",
        "evaluator_version": live.LIVE_OUTCOME_VERSION,
        "status": "pending",
        "entry_event_ts": None,
        "entry_price": None,
        "latest_event_ts": None,
        "candle_count": 0,
        "horizons": {},
    }

    live.record_live_outcome(outcome)

    statement = str(execute.call_args.args[0])
    assert "evaluated_at = NOW()" in statement
    assert "WHERE live_research_outcomes.candle_count" not in statement


def test_outcome_poll_prioritizes_subscribed_symbols_then_partial_rows(monkeypatch):
    monkeypatch.setattr(live, "ensure_schema", MagicMock())
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    execute = MagicMock(return_value=cursor)
    conn = MagicMock()
    conn.execute = execute
    context = MagicMock()
    context.__enter__.return_value = conn
    context.__exit__.return_value = False
    monkeypatch.setattr(live.psycopg, "connect", MagicMock(return_value=context))

    live.list_outcome_candidates(
        limit=100, priority_symbols=["mara", "PATH", "MARA", ""],
    )

    statement, params = execute.call_args.args
    assert "candidate.symbol = ANY" in str(statement)
    assert "COALESCE(outcome.candle_count, 0) > 0 DESC" in str(statement)
    assert params["priority_symbols"] == ["MARA", "PATH"]


def test_stream_symbols_follow_preregistered_tier_and_score_order(monkeypatch):
    monkeypatch.setattr(live, "ensure_schema", MagicMock())
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    execute = MagicMock(return_value=cursor)
    conn = MagicMock()
    conn.execute = execute
    context = MagicMock()
    context.__enter__.return_value = conn
    context.__exit__.return_value = False
    monkeypatch.setattr(live.psycopg, "connect", MagicMock(return_value=context))

    live.symbols_requiring_outcomes(limit=25, now_ts=REGISTERED_AT + 1_000)

    statement = str(execute.call_args.args[0])
    assert "MAX(priority_tier) DESC" in statement
    assert "MAX(ranking_score) DESC" in statement
    assert "MAX(signal_ts) DESC" in statement
