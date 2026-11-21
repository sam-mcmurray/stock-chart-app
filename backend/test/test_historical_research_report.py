from datetime import datetime
from unittest.mock import MagicMock
import os
import json
from zoneinfo import ZoneInfo

import pytest

from services import historical_research_report as report


EASTERN = ZoneInfo("America/New_York")


@pytest.mark.skipif(os.getenv("RESEARCH_SQL_INTEGRATION") != "1", reason="Explicit temporary-table PostgreSQL test")
def test_registered_candidate_queries_preserve_membership_and_counts(monkeypatch):
    # Temporary tables shadow production names only on this connection.
    real_connect = report.psycopg.connect
    with real_connect(os.environ["DATABASE_URL"], row_factory=report.dict_row) as conn:
        conn.execute("CREATE TEMP TABLE feature_store (id text PRIMARY KEY, source text, payload jsonb)")
        conn.execute("CREATE TEMP TABLE historical_candidate_source_versions (generator_version text, candidate_ids jsonb)")
        conn.execute("CREATE TEMP TABLE historical_candidate_outcomes (candidate_id text, evaluator_version text, symbol text, signal_ts bigint, payload jsonb)")
        conn.execute("CREATE TEMP TABLE market_observations (symbol text, observation_type text, source text, event_ts bigint, open float, close float, received_ts bigint, created_at timestamptz)")
        for i in range(6):
            row = _row(i, 0.5, float(i))
            payload = row["candidate_payload"]
            payload["candidate_generator_version"] = "g2" if i == 4 else "g1"
            payload["semantic_signal"]["hype"]["feature_version"] = "f1"
            conn.execute("INSERT INTO feature_store VALUES (%s,%s,%s::jsonb)",
                         (row["candidate_id"], "other" if i == 3 else "historical_news_hype", json.dumps(payload)))
            if i != 5:
                conn.execute("INSERT INTO historical_candidate_outcomes VALUES (%s,%s,%s,%s,%s::jsonb)",
                             (row["candidate_id"], report.EVALUATOR_VERSION, row["symbol"], row["signal_ts"], json.dumps(row["outcome_payload"])))
        # Duplicates, orphan IDs, wrong generator, wrong source and missing outcomes.
        conn.execute("INSERT INTO historical_candidate_source_versions VALUES (%s,%s::jsonb),(%s,%s::jsonb)",
                     ("g1", json.dumps(["candidate-0", "candidate-0", "candidate-1", "candidate-3", "candidate-4", "candidate-5", "orphan"]),
                      "g1", json.dumps(["candidate-0"])))
        legacy = conn.execute("""SELECT f.id FROM feature_store f
            WHERE f.source='historical_news_hype' AND EXISTS (
              SELECT 1 FROM historical_candidate_source_versions s
              WHERE s.generator_version=f.payload->>'candidate_generator_version' AND s.candidate_ids ? f.id)
            ORDER BY f.id""").fetchall()
        assert [r["id"] for r in legacy] == ["candidate-0", "candidate-1", "candidate-5"]
        class BorrowedConnection:
            def __enter__(self): return conn
            def __exit__(self, *args): pass
        monkeypatch.setattr(report.psycopg, "connect", lambda *a, **kw: BorrowedConnection())
        result = report.report(feature_version="f1", generator_version="g1")
        assert result["source_row_count"] == 2
        assert result["usable_outcome_count"] == 2
        assert result["overall"]["mean_return_pct"] == 0.5
        assert result["available_versions"] == [{"generator_version": "g1", "feature_version": "f1", "candidate_count": 3, "outcome_count": 2}]


def test_report_database_reads_have_time_limits(monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value.execute.return_value.fetchall.return_value = []
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(report, "DATABASE_URL", "test")
    monkeypatch.setattr(report.psycopg, "connect", connect)
    assert report.report()["usable_outcome_count"] == 0
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert "statement_timeout=5000" in connect.call_args.kwargs["options"]
    assert "lock_timeout=2000" in connect.call_args.kwargs["options"]


def _row(index: int, score: float, outcome: float, *, symbol: str = "NVDA"):
    signal_ts = int(datetime(2026, 9, 14 + index // 3, 10 + index % 3, tzinfo=EASTERN).timestamp())
    return {
        "candidate_id": f"candidate-{index}",
        "symbol": symbol,
        "signal_ts": signal_ts,
        "candidate_payload": {
            "source_record_type": "article",
            "semantic_signal": {"hype": {
                "long_hype_score": score,
                "catalyst": {"primary": "earnings"},
            }},
        },
        "outcome_payload": {"horizons": {"60": {
            "return_pct": outcome,
            "mfe_pct": outcome + 1,
            "mae_pct": outcome - 1,
        }}},
    }


def test_report_builds_stratified_metrics_and_walk_forward_without_future_thresholds():
    rows = [_row(index, index / 10, float(index - 4), symbol="NVDA" if index < 6 else "AMD")
            for index in range(8)]

    result = report.build_report(rows, horizon=60, folds=4)

    assert result["usable_outcome_count"] == 8
    assert result["overall"]["count"] == 8
    assert len(result["descriptive_rank_buckets"]) == 4
    assert len(result["walk_forward"]) == 3
    first = result["walk_forward"][0]
    assert first["training_count"] == 2
    assert first["training_top_quartile_threshold"] == pytest.approx(0.075)
    assert first["test_start_ts"] > first["train_end_ts"]
    assert {row["symbol"] for row in result["by_symbol"]} == {"NVDA", "AMD"}
    assert result["live_activation_eligible"] is False


def test_report_excludes_rows_without_requested_horizon():
    row = _row(0, 0.5, 2.0)
    row["outcome_payload"]["horizons"] = {"5": row["outcome_payload"]["horizons"]["60"]}

    result = report.build_report([row], horizon=60)

    assert result["usable_outcome_count"] == 0
    assert result["excluded_without_horizon_or_score"] == 1
    assert result["overall"]["mean_return_pct"] is None


def test_report_compares_returns_to_same_horizon_benchmark():
    row = _row(0, 0.5, 2.0)
    candles = []
    for index in range(60):
        candles.append({
            "event_ts": row["signal_ts"] + 60 * (index + 1),
            "open": 100.0,
            "close": 101.0 if index == 59 else 100.0,
        })

    result = report.build_report([row], horizon=60, benchmark_candles=candles)

    assert result["overall"]["benchmark_coverage_count"] == 1
    assert result["overall"]["mean_benchmark_return_pct"] == pytest.approx(1.0)
    assert result["overall"]["mean_excess_return_pct"] == pytest.approx(1.0)


def test_report_uses_version_declared_ranking_score():
    low = _row(0, 0.9, -1.0)
    high = _row(1, 0.1, 2.0)
    low["candidate_payload"]["semantic_signal"]["hype"].update({
        "candidate_priority_score": 0.2,
        "scoring_spec": {"ranking_score_field": "candidate_priority_score"},
    })
    high["candidate_payload"]["semantic_signal"]["hype"].update({
        "candidate_priority_score": 1.1,
        "scoring_spec": {"ranking_score_field": "candidate_priority_score"},
    })

    result = report.build_report([low, high], horizon=60)

    assert result["ranking_score_field"] == "candidate_priority_score"
    populated = [
        row for row in result["descriptive_rank_buckets"]
        if row["minimum_hype_score"] is not None
    ]
    assert populated[0]["minimum_hype_score"] == 0.2
    assert populated[-1]["maximum_hype_score"] == 1.1


def test_report_evaluates_zero_direction_attention_cohort_on_preregistered_mfe():
    rows = []
    for index in range(8):
        row = _row(index, 0.0, float(index) / 10)
        hype = row["candidate_payload"]["semantic_signal"]["hype"]
        hype.update({
            "candidate_priority_score": index / 10,
            "scoring_spec": {"ranking_score_field": "candidate_priority_score"},
        })
        row["outcome_payload"]["horizons"]["60"]["mfe_pct"] = float(index)
        rows.append(row)

    result = report.build_report(rows, horizon=60, folds=4)
    cohort = result["hypothesis_cohort"]

    assert cohort["count"] == 8
    assert cohort["primary_metric"] == "mean_mfe_pct"
    assert cohort["overall"]["mean_mfe_ci_95_pct"] is not None
    assert (
        cohort["descriptive_rank_buckets"][-1]["mean_mfe_pct"]
        > cohort["descriptive_rank_buckets"][0]["mean_mfe_pct"]
    )
