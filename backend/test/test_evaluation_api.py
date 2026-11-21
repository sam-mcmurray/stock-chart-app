from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from routers import evaluation_api


@pytest.fixture(autouse=True)
def direct_snapshot_build(monkeypatch):
    monkeypatch.setattr(evaluation_api.research_snapshots, "request",
                        lambda kind, parameters, builder: (builder(), False))


@pytest.mark.parametrize("error", [evaluation_api.QueryCanceled, evaluation_api.LockNotAvailable])
def test_research_timeouts_return_retryable_errors(monkeypatch, error):
    monkeypatch.setattr(evaluation_api.historical_research_report, "report", MagicMock(side_effect=error()))
    monkeypatch.setattr(evaluation_api.historical_price_import, "status", MagicMock(side_effect=error()))
    for call in (
        lambda: evaluation_api.historical_outcome_report(horizon=60, folds=4, _access_token="test"),
        lambda: evaluation_api.historical_price_status(_access_token="test"),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 503
        assert "retry" in exc.value.detail


def _signal():
    return {
        "symbol": "NVDA",
        "ts": 1_050,
        "hype": {
            "feature_version": "news-hype-v1",
            "as_of_ts": 1_050,
            "long_hype_score": 0.9,
        },
    }


def _bars():
    return [
        {
            "event_ts": 1_100,
            "received_ts": 1_160,
            "open": 10,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 100_000,
        },
        {
            "event_ts": 1_160,
            "received_ts": 1_220,
            "open": 9.7,
            "high": 9.8,
            "low": 9.6,
            "close": 9.7,
            "volume": 100_000,
        },
    ]


def test_replay_endpoint_loads_durable_inputs_and_records_run(monkeypatch):
    load_signals = MagicMock(return_value=[_signal()])
    load_market = MagicMock(return_value=_bars())
    save = MagicMock(return_value="run-123")
    monkeypatch.setattr(
        evaluation_api.persistent_store,
        "list_feature_signals_for_replay",
        load_signals,
    )
    monkeypatch.setattr(
        evaluation_api.persistent_store,
        "list_market_observation_versions",
        load_market,
    )
    monkeypatch.setattr(
        evaluation_api.persistent_store,
        "record_strategy_evaluation_run",
        save,
    )

    result = evaluation_api.replay_news_hype(
        evaluation_api.ReplayRequest(
            symbol="nvda",
            start_ts=1_000,
            end_ts=2_000,
            config={
                "assumed_spread_bps": 0,
                "entry_slippage_bps": 0,
                "exit_slippage_bps": 0,
                "entry_volume_participation_pct": 1,
                "exit_volume_participation_pct": 1,
            },
        ),
        _access_token="token",
    )

    assert result["run_id"] == "run-123"
    assert result["broker_calls"] == 0
    assert result["metrics"]["trade_count"] == 1
    load_signals.assert_called_once()
    load_market.assert_called_once()
    assert load_market.call_args.kwargs["observation_type"] is None
    save.assert_called_once()


def test_replay_endpoint_rejects_unknown_config_field():
    with pytest.raises(HTTPException) as exc:
        evaluation_api.replay_news_hype(
            evaluation_api.ReplayRequest(
                symbol="NVDA",
                start_ts=1_000,
                end_ts=2_000,
                config={"magic_accuracy": 1},
            ),
            _access_token="token",
        )

    assert exc.value.status_code == 400
    assert "magic_accuracy" in exc.value.detail


def test_historical_outcome_status_preserves_research_boundaries(monkeypatch):
    ensure = MagicMock()
    monkeypatch.setattr(evaluation_api.historical_outcome_evaluator, "ensure_schema", ensure)
    monkeypatch.setattr(
        evaluation_api.historical_outcome_evaluator,
        "status",
        MagicMock(return_value={
            "outcomes": {"complete": 12},
            "retrospective": True,
            "live_received": False,
            "live_activation_eligible": False,
        }),
    )

    result = evaluation_api.historical_outcome_status(_access_token="token")

    assert result["retrospective"] is True
    assert result["live_received"] is False
    assert result["live_activation_eligible"] is False
    ensure.assert_called_once()


def test_historical_candidate_status_selects_version(monkeypatch):
    monkeypatch.setattr(
        evaluation_api.historical_candidate_generator, "ensure_schema", MagicMock()
    )
    status = MagicMock(return_value={"candidate_count": 12})
    monkeypatch.setattr(evaluation_api.historical_candidate_generator, "status", status)

    result = evaluation_api.historical_candidate_status(
        generator_version="news-hype-candidate-generator-v2-watch-priority",
        _access_token="token",
    )

    assert result["candidate_count"] == 12
    status.assert_called_once_with("news-hype-candidate-generator-v2-watch-priority")


def test_historical_outcomes_validates_and_normalizes_symbol(monkeypatch):
    listing = MagicMock(return_value=[])
    monkeypatch.setattr(
        evaluation_api.historical_outcome_evaluator, "ensure_schema", MagicMock()
    )
    monkeypatch.setattr(
        evaluation_api.historical_outcome_evaluator, "list_outcomes", listing
    )

    result = evaluation_api.historical_outcomes(
        symbol="nvda", status="partial", limit=25, _access_token="token"
    )

    assert result == []
    listing.assert_called_once_with(symbol="NVDA", status_filter="partial", limit=25)
    with pytest.raises(HTTPException) as exc:
        evaluation_api.historical_outcomes(
            symbol="bad symbol", status=None, limit=25, _access_token="token"
        )
    assert exc.value.status_code == 400


def test_historical_outcome_report_is_read_only_and_validates_horizon(monkeypatch):
    build = MagicMock(return_value={"ranking_only": True, "live_activation_eligible": False})
    monkeypatch.setattr(evaluation_api.historical_research_report, "report", build)

    result = evaluation_api.historical_outcome_report(
        horizon=60,
        folds=5,
        feature_version="news-hype-v1",
        generator_version="news-hype-candidate-generator-v1",
        _access_token="token",
    )

    assert result["live_activation_eligible"] is False
    build.assert_called_once_with(
        horizon=60,
        folds=5,
        feature_version="news-hype-v1",
        generator_version="news-hype-candidate-generator-v1",
        statement_timeout_ms=300000,
    )
    with pytest.raises(HTTPException) as exc:
        evaluation_api.historical_outcome_report(
            horizon=30, folds=4, _access_token="token"
        )
    assert exc.value.status_code == 400


def test_registered_experiments_are_read_only(monkeypatch):
    listing = MagicMock(return_value=[{
        "experiment_version": "news-hype-watch-priority-v2",
        "status": "preregistered_research_only",
    }])
    monkeypatch.setattr(evaluation_api.research_experiments, "list_experiments", listing)

    result = evaluation_api.registered_experiments(_access_token="token")

    assert result[0]["status"] == "preregistered_research_only"
    listing.assert_called_once_with()


def test_live_experiment_status_is_read_only(monkeypatch):
    status = MagicMock(return_value={
        "candidate_count": 4, "live_received": True, "activation_eligible": False,
    })
    monkeypatch.setattr(evaluation_api.live_research_experiment, "status", status)

    result = evaluation_api.live_experiment_status(_access_token="token")

    assert result["live_received"] is True
    assert result["activation_eligible"] is False
    status.assert_called_once_with()


def test_live_experiment_report_is_read_only(monkeypatch):
    report = MagicMock(return_value={
        "readout_status": "accumulating",
        "live_received": True,
        "activation_eligible": False,
    })
    monkeypatch.setattr(evaluation_api.live_research_experiment, "live_report", report)

    result = evaluation_api.live_experiment_report(_access_token="token")

    assert result["readout_status"] == "accumulating"
    assert result["live_received"] is True
    assert result["activation_eligible"] is False
    report.assert_called_once_with()


def test_shadow_candidates_expose_reevaluation_expiry(monkeypatch):
    monkeypatch.setattr(evaluation_api.shadow_trade_store, "ensure_schema", MagicMock())
    listing = MagicMock(return_value=[{
        "candidate_id": "candidate-1", "symbol": "ABCD", "signal_ts": 1_000,
        "status": "monitoring", "reason": "entry_quote_invalid_or_stale",
        "check_count": 7,
    }])
    monkeypatch.setattr(
        evaluation_api.shadow_trade_store, "list_candidate_evaluations", listing
    )
    monkeypatch.setenv("SHADOW_CANDIDATE_MAX_AGE_SECONDS", "900")

    result = evaluation_api.shadow_candidates(
        status="monitoring", limit=25, _access_token="token"
    )

    assert result[0]["check_count"] == 7
    assert result[0]["expires_ts"] == 1_900
    listing.assert_called_once_with(status="monitoring", limit=25)


def test_shadow_opportunities_expose_durable_reevaluation_expiry(monkeypatch):
    monkeypatch.setattr(evaluation_api.shadow_trade_store, "ensure_schema", MagicMock())
    listing = MagicMock(return_value=[{
        "candidate_id": "shadow-opportunity:abc", "symbol": "ABCD",
        "signal_ts": 2_000, "status": "watching", "reason": "hype_below_threshold",
        "check_count": 3,
    }])
    monkeypatch.setattr(evaluation_api.shadow_trade_store, "list_opportunities", listing)
    monkeypatch.setenv("SHADOW_CANDIDATE_MAX_AGE_SECONDS", "900")

    result = evaluation_api.shadow_opportunities(
        status="watching", limit=25, _access_token="token"
    )

    assert result[0]["candidate_id"] == "shadow-opportunity:abc"
    assert result[0]["expires_ts"] == 2_900
    listing.assert_called_once_with(status="watching", limit=25)
