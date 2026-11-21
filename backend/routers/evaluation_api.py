from __future__ import annotations

from dataclasses import fields
import os
import re
from psycopg.errors import QueryCanceled, LockNotAvailable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from services import research_snapshots
from pydantic import BaseModel, Field

from routers.auth import require_access_token
from services import persistent_store
from services.replay_engine import ReplayConfig, run_news_hype_replay
from services import shadow_trade_store
from services import historical_candidate_generator
from services import historical_price_import
from services import historical_outcome_evaluator
from services import historical_research_report
from services import research_experiments
from services import live_research_experiment


router = APIRouter(prefix="/agent/evaluations", tags=["agent-evaluations"])


@router.get('/training-data/status')
def training_data_status(_access_token: str = Depends(require_access_token)):
    from services.training_data_status import status
    return status()


@router.get('/live-learning/status')
def live_learning_status(_access_token: str = Depends(require_access_token)):
    from services.live_learning_status import status
    return status()

SYMBOL_RE = re.compile(r"^[A-Z0-9.$/-]{1,15}$")
CONFIG_FIELDS = {field.name for field in fields(ReplayConfig)}


class ReplayRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)
    start_ts: int = Field(gt=0)
    end_ts: int = Field(gt=0)
    config: dict[str, Any] = Field(default_factory=dict)
    signals: list[dict[str, Any]] | None = None
    market_observations: list[dict[str, Any]] | None = None
    persist: bool = True


@router.post("/replay")
def replay_news_hype(
    body: ReplayRequest,
    _access_token: str = Depends(require_access_token),
):
    symbol = body.symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    if body.end_ts <= body.start_ts:
        raise HTTPException(status_code=400, detail="end_ts must be greater than start_ts")
    unknown = sorted(set(body.config) - CONFIG_FIELDS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown replay config fields: {', '.join(unknown)}",
        )
    try:
        config = ReplayConfig(**body.config)
        signals = body.signals
        if signals is None:
            signals = persistent_store.list_feature_signals_for_replay(
                symbol,
                start_ts=body.start_ts,
                end_ts=body.end_ts,
                limit=5000,
            )
        observations = body.market_observations
        if observations is None:
            observations = persistent_store.list_market_observation_versions(
                symbol,
                start_ts=body.start_ts,
                end_ts=body.end_ts,
                observation_type=None,
                limit=100_000,
            )
        result = run_news_hype_replay(
            symbol,
            signals=signals,
            market_observations=observations,
            start_ts=body.start_ts,
            end_ts=body.end_ts,
            config=config,
        )
        if body.persist:
            result["run_id"] = persistent_store.record_strategy_evaluation_run(result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/runs")
def evaluation_runs(
    symbol: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    _access_token: str = Depends(require_access_token),
):
    if symbol and not SYMBOL_RE.fullmatch(symbol.strip().upper()):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    return persistent_store.list_strategy_evaluation_runs(
        symbol=symbol.strip().upper() if symbol else None,
        limit=limit,
    )


@router.get("/shadow/summary")
def shadow_summary(_access_token: str = Depends(require_access_token)):
    shadow_trade_store.ensure_schema()
    return shadow_trade_store.summary()


@router.get("/shadow/positions")
def shadow_positions(
    state: str | None = Query(None, pattern="^(open|runner|closed)$"),
    limit: int = Query(100, ge=1, le=500),
    _access_token: str = Depends(require_access_token),
):
    shadow_trade_store.ensure_schema()
    return shadow_trade_store.list_positions(state=state, limit=limit)


@router.get("/shadow/events")
def shadow_events(
    position_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    _access_token: str = Depends(require_access_token),
):
    shadow_trade_store.ensure_schema()
    return shadow_trade_store.list_events(position_id=position_id, limit=limit)


@router.get("/shadow/candidates")
def shadow_candidates(
    status: str | None = Query(None, pattern="^(monitoring|entered|rejected|skipped)$"),
    limit: int = Query(100, ge=1, le=500),
    _access_token: str = Depends(require_access_token),
):
    shadow_trade_store.ensure_schema()
    rows = shadow_trade_store.list_candidate_evaluations(status=status, limit=limit)
    max_age = max(30, int(os.getenv("SHADOW_CANDIDATE_MAX_AGE_SECONDS", "900")))
    return [{**row, "expires_ts": int(row["signal_ts"]) + max_age} for row in rows]


@router.get("/shadow/opportunities")
def shadow_opportunities(
    status: str | None = Query(None, pattern="^(watching|monitoring|entered|expired|skipped)$"),
    limit: int = Query(100, ge=1, le=500),
    _access_token: str = Depends(require_access_token),
):
    shadow_trade_store.ensure_schema()
    rows = shadow_trade_store.list_opportunities(status=status, limit=limit)
    max_age = max(30, int(os.getenv("SHADOW_CANDIDATE_MAX_AGE_SECONDS", "900")))
    return [{**row, "expires_ts": int(row["signal_ts"]) + max_age} for row in rows]


@router.get("/historical-candidates/status")
def historical_candidate_status(
    generator_version: str = Query(
        "news-hype-candidate-generator-v1", pattern=r"^[a-z0-9._-]{1,80}$"
    ),
    _access_token: str = Depends(require_access_token),
):
    historical_candidate_generator.ensure_schema()
    if generator_version not in historical_candidate_generator.GENERATOR_SPECS:
        raise HTTPException(status_code=400, detail="Unknown candidate generator version")
    return historical_candidate_generator.status(generator_version)


@router.get("/historical-prices/status")
def historical_price_status(_access_token: str = Depends(require_access_token)):
    try:
        result, pending = research_snapshots.request("price-coverage", {"scope": "import_tasks"},
            lambda: historical_price_import.status(statement_timeout_ms=300000, include_coverage=False))
        return JSONResponse(result, status_code=202) if pending else result
    except (QueryCanceled, LockNotAvailable) as exc:
        raise HTTPException(status_code=503, detail="Historical price coverage is busy; retry shortly") from exc


@router.get("/historical-outcomes/status")
def historical_outcome_status(_access_token: str = Depends(require_access_token)):
    historical_outcome_evaluator.ensure_schema()
    return historical_outcome_evaluator.status()


@router.get("/historical-outcomes")
def historical_outcomes(
    symbol: str | None = None,
    status: str | None = Query(None, pattern="^(complete|partial|insufficient)$"),
    limit: int = Query(100, ge=1, le=500),
    _access_token: str = Depends(require_access_token),
):
    if symbol and not SYMBOL_RE.fullmatch(symbol.strip().upper()):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    historical_outcome_evaluator.ensure_schema()
    return historical_outcome_evaluator.list_outcomes(
        symbol=symbol.strip().upper() if symbol else None,
        status_filter=status,
        limit=limit,
    )


@router.get("/historical-outcomes/report")
def historical_outcome_report(
    horizon: int = Query(60),
    folds: int = Query(4, ge=2, le=10),
    feature_version: str = Query("news-hype-v1", pattern=r"^[a-z0-9._-]{1,80}$"),
    generator_version: str = Query(
        "news-hype-candidate-generator-v1", pattern=r"^[a-z0-9._-]{1,80}$"
    ),
    _access_token: str = Depends(require_access_token),
):
    if horizon not in historical_research_report.SUPPORTED_HORIZONS:
        raise HTTPException(
            status_code=400,
            detail=f"horizon must be one of {historical_research_report.SUPPORTED_HORIZONS}",
        )
    try:
        parameters = dict(horizon=horizon, folds=folds, feature_version=feature_version,
                          generator_version=generator_version)
        result, pending = research_snapshots.request("historical-report",
            {**parameters, "benchmark": historical_research_report.BENCHMARK_SYMBOL},
            lambda: historical_research_report.report(**parameters, statement_timeout_ms=300000))
        return JSONResponse(result, status_code=202) if pending else result
    except (QueryCanceled, LockNotAvailable) as exc:
        raise HTTPException(status_code=503, detail="Historical report is busy; retry shortly") from exc


@router.get("/experiments")
def registered_experiments(_access_token: str = Depends(require_access_token)):
    return research_experiments.list_experiments()


@router.get("/experiments/live/status")
def live_experiment_status(_access_token: str = Depends(require_access_token)):
    return live_research_experiment.status()


@router.get("/experiments/live/report")
def live_experiment_report(_access_token: str = Depends(require_access_token)):
    return live_research_experiment.live_report()


@router.get("/experiments/live/candidates")
def live_experiment_candidates(
    limit: int = Query(100, ge=1, le=500),
    _access_token: str = Depends(require_access_token),
):
    return live_research_experiment.list_candidates(limit=limit)
