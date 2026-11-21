from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, time as datetime_time, timezone
import math
import os
from statistics import median
from statistics import stdev
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from services.news_hype_versions import GENERATOR_VERSION, FEATURE_VERSION


DATABASE_URL = os.getenv("DATABASE_URL")
EVALUATOR_VERSION = "historical-news-outcome-v1"
EASTERN = ZoneInfo("America/New_York")
SUPPORTED_HORIZONS = (5, 15, 60, 390)
BENCHMARK_SYMBOL = os.getenv("HISTORICAL_RESEARCH_BENCHMARK_SYMBOL", "SPY").strip().upper()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _session(signal_ts: int) -> str:
    value = datetime.fromtimestamp(signal_ts, tz=timezone.utc).astimezone(EASTERN)
    if value.weekday() >= 5:
        return "weekend"
    if value.time() < datetime_time(9, 30):
        return "premarket"
    if value.time() < datetime_time(16, 0):
        return "regular"
    return "afterhours"


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [row["return_pct"] for row in rows]
    if not returns:
        return {
            "count": 0, "mean_return_pct": None, "median_return_pct": None,
            "win_rate_pct": None, "mean_mfe_pct": None, "mean_mae_pct": None,
            "mean_mfe_ci_95_pct": None, "mean_mae_ci_95_pct": None,
        }
    mean_return = sum(returns) / len(returns)
    return_margin = 1.96 * stdev(returns) / math.sqrt(len(returns)) if len(returns) > 1 else None
    wins = sum(value > 0 for value in returns)
    win_rate = wins / len(returns)
    denominator = 1 + 1.96 ** 2 / len(returns)
    center = (win_rate + 1.96 ** 2 / (2 * len(returns))) / denominator
    win_margin = 1.96 * math.sqrt(
        win_rate * (1 - win_rate) / len(returns) + 1.96 ** 2 / (4 * len(returns) ** 2)
    ) / denominator
    mfe_values = [row["mfe_pct"] for row in rows]
    mae_values = [row["mae_pct"] for row in rows]
    mean_mfe = sum(mfe_values) / len(mfe_values)
    mean_mae = sum(mae_values) / len(mae_values)
    mfe_margin = 1.96 * stdev(mfe_values) / math.sqrt(len(mfe_values)) if len(mfe_values) > 1 else None
    mae_margin = 1.96 * stdev(mae_values) / math.sqrt(len(mae_values)) if len(mae_values) > 1 else None
    result = {
        "count": len(rows),
        "mean_return_pct": mean_return,
        "mean_return_ci_95_pct": (
            [mean_return - return_margin, mean_return + return_margin]
            if return_margin is not None else None
        ),
        "median_return_pct": median(returns),
        "win_rate_pct": win_rate * 100,
        "win_rate_ci_95_pct": [(center - win_margin) * 100, (center + win_margin) * 100],
        "mean_mfe_pct": mean_mfe,
        "mean_mfe_ci_95_pct": (
            [mean_mfe - mfe_margin, mean_mfe + mfe_margin]
            if mfe_margin is not None else None
        ),
        "mean_mae_pct": mean_mae,
        "mean_mae_ci_95_pct": (
            [mean_mae - mae_margin, mean_mae + mae_margin]
            if mae_margin is not None else None
        ),
    }
    benchmarked = [row for row in rows if row.get("benchmark_return_pct") is not None]
    excess_returns = [
        row["return_pct"] - row["benchmark_return_pct"] for row in benchmarked
    ]
    mean_excess = sum(excess_returns) / len(excess_returns) if excess_returns else None
    excess_margin = (
        1.96 * stdev(excess_returns) / math.sqrt(len(excess_returns))
        if len(excess_returns) > 1 else None
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


def _attach_benchmark(
    rows: list[dict[str, Any]], candles: list[dict[str, Any]], horizon: int
) -> None:
    usable = []
    for candle in candles:
        event_ts = int(candle.get("event_ts") or 0)
        open_price = _number(candle.get("open"))
        close_price = _number(candle.get("close"))
        if event_ts > 0 and open_price and close_price and _session(event_ts) == "regular":
            usable.append((event_ts, open_price, close_price))
    usable.sort(key=lambda value: value[0])
    timestamps = [value[0] for value in usable]
    for row in rows:
        start = bisect_right(timestamps, row["signal_ts"])
        end = start + horizon - 1
        row["benchmark_return_pct"] = (
            (usable[end][2] / usable[start][1] - 1) * 100
            if start < len(usable) and end < len(usable) else None
        )


def _usable_row(row: dict[str, Any], horizon: int) -> dict[str, Any] | None:
    candidate = dict(row.get("candidate_payload") or {})
    outcome = dict(row.get("outcome_payload") or {})
    horizon_result = dict((outcome.get("horizons") or {}).get(str(horizon)) or {})
    hype = dict((candidate.get("semantic_signal") or {}).get("hype") or {})
    return_pct = _number(horizon_result.get("return_pct"))
    mfe_pct = _number(horizon_result.get("mfe_pct"))
    mae_pct = _number(horizon_result.get("mae_pct"))
    ranking_score_field = str(
        (hype.get("scoring_spec") or {}).get("ranking_score_field")
        or "long_hype_score"
    )
    hype_score = _number(hype.get(ranking_score_field))
    long_hype_score = _number(hype.get("long_hype_score"))
    if None in (return_pct, mfe_pct, mae_pct, hype_score, long_hype_score):
        return None
    return {
        "candidate_id": row["candidate_id"],
        "symbol": str(row["symbol"]).upper(),
        "signal_ts": int(row["signal_ts"]),
        "session": _session(int(row["signal_ts"])),
        "source_record_type": candidate.get("source_record_type") or "unknown",
        "feature_version": hype.get("feature_version") or "unknown",
        "generator_version": candidate.get("candidate_generator_version") or "unknown",
        "catalyst": (hype.get("catalyst") or {}).get("primary") or "unknown",
        "hype_score": hype_score,
        "ranking_score_field": ranking_score_field,
        "candidate_priority_tier": int(long_hype_score > 0),
        "return_pct": return_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
    }


def _breakdown(rows: list[dict[str, Any]], key: str, *, limit: int = 25) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    ranked = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]
    return [{key: label, **_metrics(group)} for label, group in ranked]


def _rank_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda row: (
        row["hype_score"], row["signal_ts"], row["candidate_id"]
    ))
    labels = ("bottom_quartile", "lower_middle", "upper_middle", "top_quartile")
    result = []
    for index, label in enumerate(labels):
        start = len(ranked) * index // 4
        end = len(ranked) * (index + 1) // 4
        group = ranked[start:end]
        result.append({
            "bucket": label,
            "minimum_hype_score": group[0]["hype_score"] if group else None,
            "maximum_hype_score": group[-1]["hype_score"] if group else None,
            **_metrics(group),
        })
    return result


def _walk_forward(rows: list[dict[str, Any]], folds: int) -> list[dict[str, Any]]:
    result = []
    unique_timestamps = sorted({row["signal_ts"] for row in rows})
    for fold in range(1, folds):
        train_boundary = len(unique_timestamps) * fold // folds
        test_boundary = len(unique_timestamps) * (fold + 1) // folds
        if train_boundary <= 0 or train_boundary >= len(unique_timestamps):
            continue
        test_start_ts = unique_timestamps[train_boundary]
        test_end_ts = unique_timestamps[test_boundary] if test_boundary < len(unique_timestamps) else None
        training = [row for row in rows if row["signal_ts"] < test_start_ts]
        testing = [row for row in rows if row["signal_ts"] >= test_start_ts
                   and (test_end_ts is None or row["signal_ts"] < test_end_ts)]
        if not training or not testing:
            continue
        threshold = _percentile([row["hype_score"] for row in training], 0.75)
        selected = [row for row in testing if row["hype_score"] >= threshold]
        result.append({
            "fold": fold,
            "train_start_ts": training[0]["signal_ts"],
            "train_end_ts": training[-1]["signal_ts"],
            "test_start_ts": testing[0]["signal_ts"],
            "test_end_ts": testing[-1]["signal_ts"],
            "training_count": len(training),
            "training_top_quartile_threshold": threshold,
            "test_baseline": _metrics(testing),
            "test_selected": _metrics(selected),
        })
    return result


def build_report(
    source_rows: list[dict[str, Any]], *, horizon: int = 60, folds: int = 4,
    benchmark_candles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if horizon not in SUPPORTED_HORIZONS:
        raise ValueError(f"Unsupported horizon: {horizon}")
    folds = max(2, min(int(folds), 10))
    rows = [usable for row in source_rows if (usable := _usable_row(row, horizon))]
    rows.sort(key=lambda row: (row["signal_ts"], row["candidate_id"]))
    _attach_benchmark(rows, benchmark_candles or [], horizon)

    bucket_rows = _rank_buckets(rows) if rows else []
    walk_forward = _walk_forward(rows, folds)
    zero_direction_rows = [row for row in rows if row["candidate_priority_tier"] == 0]

    return {
        "evaluator_version": EVALUATOR_VERSION,
        "horizon_bars": horizon,
        "source_row_count": len(source_rows),
        "usable_outcome_count": len(rows),
        "excluded_without_horizon_or_score": len(source_rows) - len(rows),
        "overall": _metrics(rows),
        "descriptive_rank_buckets": bucket_rows,
        "descriptive_bucket_method": "equal-count score ranks with stable tie breaking",
        "walk_forward": walk_forward,
        "by_session": _breakdown(rows, "session"),
        "by_symbol": _breakdown(rows, "symbol"),
        "by_catalyst": _breakdown(rows, "catalyst"),
        "by_source_record_type": _breakdown(rows, "source_record_type"),
        "retrospective": True,
        "live_received": False,
        "ranking_only": True,
        "live_activation_eligible": False,
        "selection_policy": "training-period hype-score 75th percentile",
        "leakage_control": "Each fold threshold uses only records earlier than its test partition.",
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "benchmark_definition": "same first later regular-session minute and bar horizon",
        "feature_version": rows[0]["feature_version"] if rows else None,
        "generator_version": rows[0]["generator_version"] if rows else None,
        "ranking_score_field": rows[0]["ranking_score_field"] if rows else None,
        "hypothesis_cohort": {
            "definition": "long_hype_score equals zero; attention is watch priority only",
            "count": len(zero_direction_rows),
            "primary_metric": "mean_mfe_pct",
            "overall": _metrics(zero_direction_rows),
            "descriptive_rank_buckets": _rank_buckets(zero_direction_rows),
            "walk_forward": _walk_forward(zero_direction_rows, folds),
            "selection_policy": "training-period cohort score 75th percentile",
        },
    }


def report(
    *, horizon: int = 60, folds: int = 4,
    feature_version: str = FEATURE_VERSION,
    generator_version: str = GENERATOR_VERSION,
    statement_timeout_ms: int = 5000,
) -> dict[str, Any]:
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5,
                         options=f"-c statement_timeout={min(300000, max(1, int(statement_timeout_ms)))} -c lock_timeout=2000") as conn:
        # Materialize membership once and give the planner the actual ID cardinality.
        # A correlated JSON membership test led to costly per-row random disk reads.
        registered = defaultdict(set)
        for source in conn.execute(
            "SELECT generator_version, candidate_ids FROM historical_candidate_source_versions"
        ).fetchall():
            registered[source["generator_version"]].update(
                value for value in source["candidate_ids"] if isinstance(value, str))
        ids = sorted(set().union(*registered.values())) if registered else []
        features = conn.execute(
            "SELECT id, payload FROM feature_store WHERE source = 'historical_news_hype' AND id = ANY(%s)",
            (ids,),
        ).fetchall() if ids else []
        outcomes = {row["candidate_id"]: row for row in conn.execute(
            """SELECT candidate_id, symbol, signal_ts, payload FROM historical_candidate_outcomes
               WHERE evaluator_version = %s AND candidate_id = ANY(%s)""",
            (EVALUATOR_VERSION, ids),
        ).fetchall()} if ids else {}
        rows, version_counts = [], {}
        for feature in features:
            candidate_id, payload = feature["id"], feature["payload"]
            generator = payload.get("candidate_generator_version")
            if candidate_id not in registered.get(generator, set()):
                continue
            version = ((payload.get("semantic_signal") or {}).get("hype") or {}).get("feature_version")
            counts = version_counts.setdefault((generator, version), {
                "generator_version": generator, "feature_version": version,
                "candidate_count": 0, "outcome_count": 0})
            outcome = outcomes.get(candidate_id)
            counts["candidate_count"] += 1
            counts["outcome_count"] += int(outcome is not None)
            if outcome and generator == generator_version and version == feature_version:
                rows.append({"candidate_id": candidate_id, "symbol": outcome["symbol"],
                             "signal_ts": outcome["signal_ts"], "candidate_payload": payload,
                             "outcome_payload": outcome["payload"]})
        versions = [version_counts[key] for key in sorted(version_counts,
                    key=lambda key: tuple((value is None, value or "") for value in key))]
        benchmark_candles = conn.execute(
            """
            SELECT event_ts, open, close FROM (
                SELECT DISTINCT ON (event_ts) event_ts, open, close, received_ts, created_at
                FROM market_observations
                WHERE symbol = %(symbol)s
                  AND observation_type = 'historical_minute_candle'
                  AND source = 'schwab_price_history'
                ORDER BY event_ts, received_ts DESC, created_at DESC
            ) AS latest ORDER BY event_ts
            """,
            {"symbol": BENCHMARK_SYMBOL},
        ).fetchall()
    result = build_report(
        [dict(row) for row in rows], horizon=horizon, folds=folds,
        benchmark_candles=[dict(row) for row in benchmark_candles],
    )
    result["feature_version"] = feature_version
    result["generator_version"] = generator_version
    result["available_versions"] = [dict(row) for row in versions]
    return result
