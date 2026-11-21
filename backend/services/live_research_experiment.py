from __future__ import annotations

import hashlib
import json
import os
import time
from functools import lru_cache
import math
from statistics import stdev
from typing import Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import psycopg
from psycopg.rows import dict_row

from services.news_hype import derive_watch_priority_features
from services.research_experiments import (
    EXPERIMENT_SPECS, WATCH_PRIORITY_EXPERIMENT, ensure_schema as ensure_experiment_schema,
)


DATABASE_URL = os.getenv("DATABASE_URL")
_SCHEMA_READY = False
EASTERN = ZoneInfo("America/New_York")
XNYS = xcals.get_calendar("XNYS")
LIVE_OUTCOME_VERSION = "live-news-outcome-v1"
LIVE_COLLECTION_SCHEDULER_VERSION = "live-stream-priority-v2"
# First deployment of supervised refresh and preregistered tier/score ordering.
LIVE_COLLECTION_SCHEDULER_STARTED_AT = 1_790_122_900
LIVE_HORIZONS = (5, 15, 60)
LIVE_BENCHMARK_SYMBOL = os.getenv("LIVE_RESEARCH_BENCHMARK_SYMBOL", "SPY").strip().upper()
LIVE_CANDIDATE_RETENTION_SECONDS = max(
    86_400, int(os.getenv("LIVE_RESEARCH_CANDIDATE_RETENTION_SECONDS", "604800"))
)
LIVE_ENTRY_GRACE_SECONDS = max(
    60, int(os.getenv("LIVE_RESEARCH_ENTRY_GRACE_SECONDS", "300"))
)
LIVE_OPEN_GRACE_SECONDS = max(
    60, int(os.getenv("LIVE_RESEARCH_OPEN_GRACE_SECONDS", "600"))
)

DDL = """
CREATE TABLE IF NOT EXISTS live_research_candidates (
    candidate_id TEXT PRIMARY KEY,
    experiment_version TEXT NOT NULL,
    episode_key TEXT NOT NULL,
    source_feature_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_ts BIGINT NOT NULL,
    received_ts BIGINT NOT NULL,
    feature_version TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    priority_tier INTEGER NOT NULL,
    ranking_score DOUBLE PRECISION NOT NULL,
    evidence_ids JSONB NOT NULL,
    payload JSONB NOT NULL,
    sightings INTEGER NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (experiment_version, episode_key)
);
CREATE INDEX IF NOT EXISTS idx_live_research_candidates_experiment_signal
    ON live_research_candidates (experiment_version, signal_ts DESC);
CREATE INDEX IF NOT EXISTS idx_live_research_candidates_symbol_signal
    ON live_research_candidates (symbol, signal_ts DESC);
CREATE TABLE IF NOT EXISTS live_research_outcomes (
    candidate_id TEXT PRIMARY KEY REFERENCES live_research_candidates(candidate_id),
    evaluator_version TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_event_ts BIGINT,
    entry_price DOUBLE PRECISION,
    latest_event_ts BIGINT,
    candle_count INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_live_research_outcomes_status
    ON live_research_outcomes (status, evaluated_at);
"""


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured")
    ensure_experiment_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(741852968)")
        conn.execute(DDL)
    _SCHEMA_READY = True


def _episode_key(symbol: str, evidence_ids: list[str]) -> str:
    raw = json.dumps(
        {"symbol": symbol, "evidence_ids": evidence_ids},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def record_scanner_candidate(
    source_snapshot: dict[str, Any], *, received_ts: int | None = None,
) -> dict[str, Any]:
    spec = EXPERIMENT_SPECS[WATCH_PRIORITY_EXPERIMENT]
    now = int(received_ts or time.time())
    payload = dict(source_snapshot.get("payload") or {})
    signal_ts = int(source_snapshot.get("ts") or payload.get("ts") or 0)
    symbol = str(source_snapshot.get("symbol") or payload.get("symbol") or "").upper()
    if source_snapshot.get("source") != "scanner_shadow" or not symbol or signal_ts <= 0:
        return {"recorded": False, "reason": "not_live_scanner_snapshot"}
    if signal_ts < spec.registered_at or now < spec.registered_at:
        return {"recorded": False, "reason": "before_registration_boundary"}
    baseline = dict((payload.get("semantic_signal") or {}).get("hype") or {})
    evidence_ids = sorted({
        str(value).strip() for value in baseline.get("evidence_article_ids") or []
        if str(value).strip()
    })
    if not evidence_ids:
        return {"recorded": False, "reason": "no_verified_article_evidence"}
    derived = derive_watch_priority_features(baseline)
    episode_key = _episode_key(symbol, evidence_ids)
    candidate_id = hashlib.sha256(
        f"{spec.experiment_version}|{episode_key}".encode()
    ).hexdigest()
    frozen_payload = {
        "experiment_version": spec.experiment_version,
        "source_feature_id": source_snapshot["id"],
        "symbol": symbol,
        "signal_ts": signal_ts,
        "received_ts": now,
        "live_received": True,
        "retrospective": False,
        "ranking_only": True,
        "entry_eligible": False,
        "live_activation_eligible": False,
        "baseline_hype": baseline,
        "experimental_hype": derived,
    }
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            INSERT INTO live_research_candidates (
                candidate_id, experiment_version, episode_key, source_feature_id,
                symbol, signal_ts, received_ts, feature_version, generator_version,
                priority_tier, ranking_score, evidence_ids, payload
            ) VALUES (
                %(candidate_id)s, %(experiment_version)s, %(episode_key)s,
                %(source_feature_id)s, %(symbol)s, %(signal_ts)s, %(received_ts)s,
                %(feature_version)s, %(generator_version)s, %(priority_tier)s,
                %(ranking_score)s, %(evidence_ids)s, %(payload)s
            )
            ON CONFLICT (experiment_version, episode_key) DO UPDATE SET
                sightings = live_research_candidates.sightings + 1,
                last_seen_at = NOW()
            RETURNING candidate_id, (xmax = 0) AS inserted, sightings
            """,
            {
                "candidate_id": candidate_id,
                "experiment_version": spec.experiment_version,
                "episode_key": episode_key,
                "source_feature_id": source_snapshot["id"],
                "symbol": symbol,
                "signal_ts": signal_ts,
                "received_ts": now,
                "feature_version": spec.feature_version,
                "generator_version": spec.generator_version,
                "priority_tier": int(derived["candidate_priority_tier"]),
                "ranking_score": float(derived["candidate_priority_score"]),
                "evidence_ids": psycopg.types.json.Jsonb(evidence_ids),
                "payload": psycopg.types.json.Jsonb(frozen_payload),
            },
        ).fetchone()
    return {
        "recorded": True,
        "candidate_id": row["candidate_id"],
        "inserted": bool(row["inserted"]),
        "sightings": int(row["sightings"]),
    }


def status(experiment_version: str = WATCH_PRIORITY_EXPERIMENT) -> dict[str, Any]:
    ensure_schema()
    spec = EXPERIMENT_SPECS[experiment_version]
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS candidate_count,
                   COUNT(*) FILTER (WHERE priority_tier = 0) AS cohort_count,
                   COUNT(DISTINCT symbol) AS symbol_count,
                   COALESCE(SUM(sightings), 0) AS sightings,
                   COUNT(*) FILTER (
                       WHERE priority_tier = 0 AND outcome.status = 'complete'
                   ) AS cohort_outcome_count,
                   COUNT(outcome.candidate_id) FILTER (
                       WHERE outcome.status = 'complete'
                   ) AS complete_outcome_count,
                   COUNT(*) FILTER (WHERE outcome.candidate_id IS NULL) AS unevaluated_count,
                   COUNT(*) FILTER (WHERE outcome.status = 'pending') AS pending_outcome_count,
                   COUNT(*) FILTER (WHERE outcome.status = 'partial') AS partial_outcome_count,
                   COUNT(*) FILTER (WHERE outcome.status = 'insufficient') AS insufficient_outcome_count,
                   COUNT(*) FILTER (
                       WHERE candidate.signal_ts >= %(scheduler_started_at)s
                   ) AS scheduler_candidate_count,
                   COUNT(*) FILTER (
                       WHERE candidate.signal_ts >= %(scheduler_started_at)s
                         AND outcome.status = 'complete'
                   ) AS scheduler_complete_count,
                   COUNT(*) FILTER (
                       WHERE candidate.signal_ts >= %(scheduler_started_at)s
                         AND outcome.status IN ('pending', 'partial')
                   ) AS scheduler_collecting_count,
                   COUNT(*) FILTER (
                       WHERE candidate.signal_ts >= %(scheduler_started_at)s
                         AND outcome.status = 'insufficient'
                   ) AS scheduler_insufficient_count,
                   COUNT(*) FILTER (
                       WHERE candidate.signal_ts >= %(scheduler_started_at)s
                         AND outcome.payload->>'reason' = 'missed_live_entry_window'
                   ) AS scheduler_late_candle_count,
                   COUNT(*) FILTER (
                       WHERE candidate.signal_ts >= %(scheduler_started_at)s
                         AND outcome.payload->>'reason' = 'entry_window_elapsed_without_candle'
                   ) AS scheduler_no_candle_count,
                   MIN(signal_ts) AS first_signal_ts,
                   MAX(signal_ts) AS latest_signal_ts
            FROM live_research_candidates candidate
            LEFT JOIN live_research_outcomes outcome
              ON outcome.candidate_id = candidate.candidate_id
             AND outcome.evaluator_version = %(evaluator_version)s
            WHERE experiment_version = %(version)s
            """,
            {
                "version": experiment_version,
                "evaluator_version": LIVE_OUTCOME_VERSION,
                "scheduler_started_at": LIVE_COLLECTION_SCHEDULER_STARTED_AT,
            },
        ).fetchone()
    cohort_count = int(row["cohort_count"] or 0)
    cohort_outcome_count = int(row["cohort_outcome_count"] or 0)
    required = spec.minimum_post_registration_live_received_sample
    return {
        "experiment_version": experiment_version,
        "experiment_status": spec.status,
        "registered_at": spec.registered_at,
        "candidate_count": int(row["candidate_count"] or 0),
        "hypothesis_cohort_count": cohort_count,
        "hypothesis_cohort_outcome_count": cohort_outcome_count,
        "complete_outcome_count": int(row["complete_outcome_count"] or 0),
        "unevaluated_count": int(row["unevaluated_count"] or 0),
        "pending_outcome_count": int(row["pending_outcome_count"] or 0),
        "partial_outcome_count": int(row["partial_outcome_count"] or 0),
        "insufficient_outcome_count": int(row["insufficient_outcome_count"] or 0),
        "distinct_symbol_count": int(row["symbol_count"] or 0),
        "sightings": int(row["sightings"] or 0),
        "first_signal_ts": row["first_signal_ts"],
        "latest_signal_ts": row["latest_signal_ts"],
        "minimum_live_received_sample": required,
        "sample_progress_pct": min(100.0, cohort_outcome_count / required * 100),
        "collection_scheduler": {
            "version": LIVE_COLLECTION_SCHEDULER_VERSION,
            "started_at": LIVE_COLLECTION_SCHEDULER_STARTED_AT,
            "candidate_count": int(row["scheduler_candidate_count"] or 0),
            "complete_count": int(row["scheduler_complete_count"] or 0),
            "collecting_count": int(row["scheduler_collecting_count"] or 0),
            "insufficient_count": int(row["scheduler_insufficient_count"] or 0),
            "late_candle_count": int(row["scheduler_late_candle_count"] or 0),
            "no_candle_count": int(row["scheduler_no_candle_count"] or 0),
        },
        "outcome_collection_ready": True,
        "activation_eligible": False,
        "live_received": True,
        "retrospective": False,
    }


def _live_metrics(rows: list[dict[str, float]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "mean_mfe_pct": None,
            "mean_mfe_ci_95_pct": None,
            "mean_return_pct": None,
            "mean_mae_pct": None,
            "win_rate_pct": None,
            "benchmark_coverage_count": 0,
            "mean_benchmark_return_pct": None,
            "mean_excess_return_pct": None,
            "mean_excess_return_ci_95_pct": None,
        }
    result = {"count": len(rows)}
    for field in ("mfe_pct", "return_pct", "mae_pct"):
        values = [row[field] for row in rows]
        mean = sum(values) / len(values)
        margin = 1.96 * stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None
        result[f"mean_{field}"] = mean
        if field == "mfe_pct":
            result["mean_mfe_ci_95_pct"] = (
                [mean - margin, mean + margin] if margin is not None else None
            )
    result["win_rate_pct"] = sum(row["return_pct"] > 0 for row in rows) / len(rows) * 100
    benchmarked = [
        row for row in rows if row.get("benchmark_return_pct") is not None
    ]
    excess = [
        row["return_pct"] - row["benchmark_return_pct"] for row in benchmarked
    ]
    mean_excess = sum(excess) / len(excess) if excess else None
    excess_margin = (
        1.96 * stdev(excess) / math.sqrt(len(excess)) if len(excess) > 1 else None
    )
    result.update({
        "benchmark_coverage_count": len(benchmarked),
        "mean_benchmark_return_pct": (
            sum(row["benchmark_return_pct"] for row in benchmarked) / len(benchmarked)
            if benchmarked else None
        ),
        "mean_excess_return_pct": mean_excess,
        "mean_excess_return_ci_95_pct": (
            [mean_excess - excess_margin, mean_excess + excess_margin]
            if mean_excess is not None and excess_margin is not None else None
        ),
    })
    return result


def build_live_report(
    source_rows: list[dict[str, Any]],
    *, experiment_version: str = WATCH_PRIORITY_EXPERIMENT,
) -> dict[str, Any]:
    spec = EXPERIMENT_SPECS[experiment_version]
    horizon_key = str(spec.horizon_bars)
    usable = []
    for source in source_rows:
        outcome = dict(source.get("outcome_payload") or {})
        horizon = dict((outcome.get("horizons") or {}).get(horizon_key) or {})
        try:
            values = {
                key: float(horizon[key])
                for key in ("mfe_pct", "return_pct", "mae_pct")
            }
            ranking_score = float(source["ranking_score"])
            priority_tier = int(source["priority_tier"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (*values.values(), ranking_score)):
            continue
        if outcome.get("entry_timing_valid") is not True:
            continue
        benchmark_return = horizon.get("benchmark_return_pct")
        try:
            benchmark_return = (
                float(benchmark_return) if benchmark_return is not None else None
            )
        except (TypeError, ValueError):
            benchmark_return = None
        if benchmark_return is not None and not math.isfinite(benchmark_return):
            benchmark_return = None
        usable.append({
            **values,
            "benchmark_return_pct": benchmark_return,
            "ranking_score": ranking_score,
            "priority_tier": priority_tier,
            "signal_ts": int(source.get("signal_ts") or 0),
            "candidate_id": str(source.get("candidate_id") or ""),
        })

    cohort = [row for row in usable if row["priority_tier"] == 0]
    ranked = sorted(
        cohort,
        key=lambda row: (row["ranking_score"], row["signal_ts"], row["candidate_id"]),
    )
    top_start = len(ranked) * 3 // 4
    top_quartile = ranked[top_start:] if ranked else []
    cohort_metrics = _live_metrics(cohort)
    top_metrics = _live_metrics(top_quartile)
    required = spec.minimum_post_registration_live_received_sample
    complete = len(cohort)
    return {
        "experiment_version": experiment_version,
        "evaluator_version": LIVE_OUTCOME_VERSION,
        "horizon_bars": spec.horizon_bars,
        "primary_metric": spec.primary_metric,
        "benchmark_symbol": LIVE_BENCHMARK_SYMBOL,
        "benchmark_definition": "live-received first later regular-session minute and equal bar horizon",
        "source_complete_count": len(source_rows),
        "usable_timing_valid_count": len(usable),
        "hypothesis_cohort": cohort_metrics,
        "top_attention_quartile": {
            **top_metrics,
            "minimum_ranking_score": top_quartile[0]["ranking_score"] if top_quartile else None,
            "comparison": "descriptive top quartile within the locked zero-direction cohort",
        },
        "top_vs_cohort_mean_mfe_difference_pct": (
            top_metrics["mean_mfe_pct"] - cohort_metrics["mean_mfe_pct"]
            if top_metrics["mean_mfe_pct"] is not None
            and cohort_metrics["mean_mfe_pct"] is not None else None
        ),
        "minimum_live_received_sample": required,
        "sample_progress_pct": min(100.0, complete / required * 100),
        "formal_readout_available": complete >= required,
        "readout_status": "sample_ready" if complete >= required else "accumulating",
        "retrospective": False,
        "live_received": True,
        "ranking_only": True,
        "activation_eligible": False,
        "generated_at": int(time.time()),
    }


def live_report(experiment_version: str = WATCH_PRIORITY_EXPERIMENT) -> dict[str, Any]:
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT candidate.candidate_id, candidate.signal_ts,
                   candidate.priority_tier, candidate.ranking_score,
                   outcome.payload AS outcome_payload
            FROM live_research_candidates candidate
            JOIN live_research_outcomes outcome
              ON outcome.candidate_id = candidate.candidate_id
             AND outcome.evaluator_version = %(evaluator_version)s
             AND outcome.status = 'complete'
            WHERE candidate.experiment_version = %(experiment_version)s
            ORDER BY candidate.signal_ts, candidate.candidate_id
            """,
            {
                "evaluator_version": LIVE_OUTCOME_VERSION,
                "experiment_version": experiment_version,
            },
        ).fetchall()
    return build_live_report([dict(row) for row in rows], experiment_version=experiment_version)


def list_candidates(
    experiment_version: str = WATCH_PRIORITY_EXPERIMENT, *, limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT candidate_id, experiment_version, source_feature_id, symbol,
                   signal_ts, received_ts, feature_version, generator_version,
                   priority_tier, ranking_score, evidence_ids, sightings,
                   last_seen_at, payload
            FROM live_research_candidates
            WHERE experiment_version = %(version)s
            ORDER BY signal_ts DESC, candidate_id
            LIMIT %(limit)s
            """,
            {"version": experiment_version, "limit": max(1, min(int(limit), 500))},
        ).fetchall()
    return [dict(row) for row in rows]


def symbols_requiring_outcomes(*, limit: int = 25, now_ts: int | None = None) -> list[str]:
    ensure_schema()
    cutoff = int(now_ts or time.time()) - LIVE_CANDIDATE_RETENTION_SECONDS
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT symbol, MAX(priority_tier) AS priority_tier,
                   MAX(ranking_score) AS ranking_score,
                   MAX(signal_ts) AS latest_signal
            FROM live_research_candidates candidate
            LEFT JOIN live_research_outcomes outcome
              ON outcome.candidate_id = candidate.candidate_id
             AND outcome.evaluator_version = %(evaluator_version)s
            WHERE candidate.experiment_version = %(experiment_version)s
              AND candidate.signal_ts >= %(cutoff)s
              AND COALESCE(outcome.status, 'pending') NOT IN ('complete', 'insufficient')
            GROUP BY symbol
            ORDER BY MAX(COALESCE(outcome.candle_count, 0)) > 0 DESC,
                     MAX(priority_tier) DESC,
                     MAX(ranking_score) DESC,
                     MAX(signal_ts) DESC,
                     symbol
            LIMIT %(limit)s
            """,
            {
                "evaluator_version": LIVE_OUTCOME_VERSION,
                "experiment_version": WATCH_PRIORITY_EXPERIMENT,
                "cutoff": cutoff,
                "limit": max(1, min(int(limit), 100)),
            },
        ).fetchall()
    return [str(row["symbol"]) for row in rows]


def list_outcome_candidates(
    *, limit: int = 100, priority_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    ensure_schema()
    prioritized = list(dict.fromkeys(
        str(symbol or "").strip().upper()
        for symbol in (priority_symbols or [])
        if str(symbol or "").strip()
    ))
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT candidate.candidate_id, candidate.symbol, candidate.signal_ts,
                   candidate.received_ts, candidate.priority_tier,
                   candidate.ranking_score, candidate.payload
            FROM live_research_candidates candidate
            LEFT JOIN live_research_outcomes outcome
              ON outcome.candidate_id = candidate.candidate_id
             AND outcome.evaluator_version = %(evaluator_version)s
            WHERE candidate.experiment_version = %(experiment_version)s
              AND COALESCE(outcome.status, 'pending') NOT IN ('complete', 'insufficient')
            ORDER BY candidate.symbol = ANY(%(priority_symbols)s::text[]) DESC,
                     COALESCE(outcome.candle_count, 0) > 0 DESC,
                     outcome.evaluated_at ASC NULLS FIRST,
                     candidate.signal_ts, candidate.candidate_id
            LIMIT %(limit)s
            """,
            {
                "evaluator_version": LIVE_OUTCOME_VERSION,
                "experiment_version": WATCH_PRIORITY_EXPERIMENT,
                "priority_symbols": prioritized,
                "limit": max(1, min(int(limit), 500)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def load_live_candles(
    candidate: dict[str, Any], *, limit: int = 500, symbol: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema()
    # Research reads must release resources under contention, not stall ingestion.
    with psycopg.connect(
        DATABASE_URL, row_factory=dict_row, connect_timeout=5,
        options="-c statement_timeout=30000 -c lock_timeout=2000",
    ) as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (event_ts)
                       observation_key, event_ts, received_ts, open, high, low,
                       close, volume, payload
                FROM market_observations
                WHERE symbol = %(symbol)s
                  AND observation_type = 'minute_candle'
                  AND source = 'schwab_stream'
                  AND event_ts > %(signal_ts)s
                  AND received_ts >= %(received_ts)s
                ORDER BY event_ts, received_ts DESC, created_at DESC
            ) latest
            ORDER BY event_ts
            LIMIT %(limit)s
            """,
            {
                "symbol": str(symbol or candidate["symbol"]).upper(),
                "signal_ts": int(candidate["signal_ts"]),
                "received_ts": int(candidate["received_ts"]),
                "limit": max(1, min(int(limit), 2000)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def load_live_benchmark_candles(
    candidate: dict[str, Any], *, limit: int = 500,
) -> list[dict[str, Any]]:
    return load_live_candles(
        candidate, limit=limit, symbol=LIVE_BENCHMARK_SYMBOL,
    )


@lru_cache(maxsize=512)
def _session_bounds(session_date: str) -> tuple[int, int] | None:
    label = pd.Timestamp(session_date)
    if not XNYS.is_session(label):
        return None
    return (
        int(XNYS.session_open(label).timestamp()),
        int(XNYS.session_close(label).timestamp()),
    )


def _regular_minute(event_ts: int) -> bool:
    value = datetime.fromtimestamp(event_ts, tz=timezone.utc).astimezone(EASTERN)
    bounds = _session_bounds(value.date().isoformat())
    return bool(bounds and bounds[0] <= event_ts < bounds[1])


def _next_session_open(local_date, *, include_date: bool) -> int:
    label = pd.Timestamp(local_date.isoformat())
    direction = "next"
    if include_date and XNYS.is_session(label):
        session = label
    else:
        if XNYS.is_session(label):
            session = XNYS.next_session(label)
        else:
            session = XNYS.date_to_session(label, direction=direction)
    return int(XNYS.session_open(session).timestamp())


def _entry_window(signal_ts: int) -> tuple[int, int, str]:
    signal = datetime.fromtimestamp(signal_ts, tz=timezone.utc).astimezone(EASTERN)
    bounds = _session_bounds(signal.date().isoformat())
    if bounds and bounds[0] <= signal_ts < bounds[1] - 60:
        return signal_ts, signal_ts + LIVE_ENTRY_GRACE_SECONDS, "immediate_regular_session"

    before_open = bool(bounds and signal_ts < bounds[0])
    start = _next_session_open(signal.date(), include_date=before_open)
    return start, start + LIVE_OPEN_GRACE_SECONDS, "next_regular_open"


def _entry_timing(signal_ts: int, entry_ts: int) -> dict[str, Any]:
    window_start, window_end, expected = _entry_window(signal_ts)
    return {
        "entry_delay_seconds": max(0, entry_ts - signal_ts),
        "entry_window_start_ts": window_start,
        "entry_window_end_ts": window_end,
        "entry_timing": expected,
        "entry_timing_valid": window_start <= entry_ts <= window_end,
    }


def evaluate_live_outcome(
    candidate: dict[str, Any], candles: list[dict[str, Any]],
    *, horizons: tuple[int, ...] = LIVE_HORIZONS, now_ts: int | None = None,
    benchmark_candles: list[dict[str, Any]] | None = None,
    benchmark_symbol: str = LIVE_BENCHMARK_SYMBOL,
) -> dict[str, Any]:
    usable = []
    for candle in candles:
        try:
            event_ts = int(candle.get("event_ts") or 0)
            values = {key: float(candle.get(key) or 0) for key in ("open", "high", "low", "close")}
            volume = max(0.0, float(candle.get("volume") or 0))
        except (TypeError, ValueError):
            continue
        if (
            event_ts <= int(candidate["signal_ts"])
            or not _regular_minute(event_ts)
            or min(values.values()) <= 0
            or values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
        ):
            continue
        usable.append({**candle, **values, "event_ts": event_ts, "volume": volume})
    usable.sort(key=lambda row: row["event_ts"])
    benchmark_usable = []
    for candle in benchmark_candles or []:
        try:
            event_ts = int(candle.get("event_ts") or 0)
            open_price = float(candle.get("open") or 0)
            close_price = float(candle.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if (
            event_ts <= int(candidate["signal_ts"])
            or not _regular_minute(event_ts)
            or open_price <= 0
            or close_price <= 0
        ):
            continue
        benchmark_usable.append({
            "event_ts": event_ts, "open": open_price, "close": close_price,
        })
    benchmark_usable.sort(key=lambda row: row["event_ts"])
    base = {
        "evaluator_version": LIVE_OUTCOME_VERSION,
        "candidate_id": candidate["candidate_id"],
        "symbol": candidate["symbol"],
        "signal_ts": int(candidate["signal_ts"]),
        "received_ts": int(candidate["received_ts"]),
        "source": "schwab_stream",
        "retrospective": False,
        "live_received": True,
        "ranking_only": True,
        "live_activation_eligible": False,
    }
    if not usable:
        window_start, window_end, expected = _entry_window(int(candidate["signal_ts"]))
        if int(now_ts or time.time()) > window_end:
            return {
                **base,
                "status": "insufficient",
                "reason": "entry_window_elapsed_without_candle",
                "entry_event_ts": None,
                "entry_price": None,
                "latest_event_ts": None,
                "candle_count": 0,
                "horizons": {},
                "entry_delay_seconds": None,
                "entry_window_start_ts": window_start,
                "entry_window_end_ts": window_end,
                "entry_timing": expected,
                "entry_timing_valid": False,
            }
        return {**base, "status": "pending", "entry_event_ts": None,
                "entry_price": None, "latest_event_ts": None,
                "candle_count": 0, "horizons": {}}
    entry = usable[0]
    entry_price = float(entry["open"])
    entry_timing = _entry_timing(int(candidate["signal_ts"]), int(entry["event_ts"]))
    if not entry_timing["entry_timing_valid"]:
        return {
            **base,
            **entry_timing,
            "status": "insufficient",
            "reason": "missed_live_entry_window",
            "entry_event_ts": int(entry["event_ts"]),
            "entry_price": entry_price,
            "latest_event_ts": int(usable[-1]["event_ts"]),
            "candle_count": len(usable),
            "horizons": {},
        }
    results = {}
    for horizon in sorted(set(int(value) for value in horizons if int(value) > 0)):
        if len(usable) < horizon:
            continue
        window = usable[:horizon]
        final = window[-1]
        results[str(horizon)] = {
            "bars": horizon,
            "end_event_ts": int(final["event_ts"]),
            "close": float(final["close"]),
            "return_pct": (float(final["close"]) / entry_price - 1) * 100,
            "mfe_pct": (max(float(row["high"]) for row in window) / entry_price - 1) * 100,
            "mae_pct": (min(float(row["low"]) for row in window) / entry_price - 1) * 100,
            "volume": sum(float(row["volume"]) for row in window),
            "ending_observation_key": final.get("observation_key"),
        }
        if (
            len(benchmark_usable) >= horizon
            and benchmark_usable[0]["event_ts"] == int(entry["event_ts"])
        ):
            benchmark_window = benchmark_usable[:horizon]
            benchmark_return = (
                benchmark_window[-1]["close"] / benchmark_window[0]["open"] - 1
            ) * 100
            results[str(horizon)].update({
                "benchmark_symbol": benchmark_symbol,
                "benchmark_return_pct": benchmark_return,
                "excess_return_pct": results[str(horizon)]["return_pct"] - benchmark_return,
            })
    status_value = "complete" if str(max(horizons)) in results else "partial"
    return {
        **base, **entry_timing, "status": status_value,
        "entry_event_ts": int(entry["event_ts"]), "entry_price": entry_price,
        "latest_event_ts": int(usable[-1]["event_ts"]),
        "candle_count": len(usable), "horizons": results,
    }


def record_live_outcome(outcome: dict[str, Any]) -> None:
    ensure_schema()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO live_research_outcomes (
                candidate_id, evaluator_version, status, entry_event_ts,
                entry_price, latest_event_ts, candle_count, payload
            ) VALUES (
                %(candidate_id)s, %(evaluator_version)s, %(status)s,
                %(entry_event_ts)s, %(entry_price)s, %(latest_event_ts)s,
                %(candle_count)s, %(payload)s
            ) ON CONFLICT (candidate_id) DO UPDATE SET
                status = EXCLUDED.status,
                entry_event_ts = EXCLUDED.entry_event_ts,
                entry_price = EXCLUDED.entry_price,
                latest_event_ts = EXCLUDED.latest_event_ts,
                candle_count = EXCLUDED.candle_count,
                payload = EXCLUDED.payload,
                evaluated_at = NOW()
            """,
            {**outcome, "payload": psycopg.types.json.Jsonb(outcome)},
        )
