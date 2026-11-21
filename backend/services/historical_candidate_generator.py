from __future__ import annotations

from dataclasses import dataclass
import os
from types import MappingProxyType
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services import persistent_store
from services.feature_store import record_feature_snapshot
from services.news_hype import build_news_hype_features
from services.research_experiments import WATCH_PRIORITY_EXPERIMENT
from services.news_hype_versions import GENERATOR_VERSION, WATCH_PRIORITY_GENERATOR_VERSION


DATABASE_URL = os.getenv("DATABASE_URL")


@dataclass(frozen=True)
class CandidateGeneratorSpec:
    generator_version: str
    feature_version: str


GENERATOR_SPECS = MappingProxyType({
    GENERATOR_VERSION: CandidateGeneratorSpec(
        generator_version=GENERATOR_VERSION,
        feature_version="news-hype-v1",
    ),
    WATCH_PRIORITY_GENERATOR_VERSION: CandidateGeneratorSpec(
        generator_version=WATCH_PRIORITY_GENERATOR_VERSION,
        feature_version="news-hype-v2-watch-priority",
    ),
})
ACCEPTED_RELEVANCE = ("finance", "business", "markets")
ACCEPTED_VERIFICATION = ("canonical", "publisher", "mention", "gdelt_mention")

# Keep runtime thresholds and classifications out of the partial predicate so
# prepared queries with different lag limits can use this index.
CANDIDATE_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_news_enrichment_versions_candidate_lag
    ON news_enrichment_versions ((observed_ts - published_ts))
    WHERE published_ts > 0
      AND observed_ts >= published_ts
      AND COALESCE((result->>'entity_link_version')::integer, 0) >= 2
      AND jsonb_array_length(COALESCE(result->'related_tickers', '[]'::jsonb)) > 0;
"""

DDL = """
CREATE TABLE IF NOT EXISTS historical_candidate_sources (
    version_key TEXT PRIMARY KEY,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    published_ts BIGINT NOT NULL,
    observed_ts BIGINT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    candidate_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    generator_version TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_historical_candidate_sources_status_observed
    ON historical_candidate_sources (status, observed_ts DESC);
CREATE TABLE IF NOT EXISTS historical_candidate_source_versions (
    version_key TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    published_ts BIGINT NOT NULL,
    observed_ts BIGINT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    candidate_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (version_key, generator_version)
);
CREATE INDEX IF NOT EXISTS idx_historical_candidate_source_versions_status_observed
    ON historical_candidate_source_versions (generator_version, status, observed_ts DESC);
CREATE INDEX IF NOT EXISTS idx_news_enrichment_versions_published_observed
    ON news_enrichment_versions (published_ts DESC, observed_ts DESC);
ALTER TABLE historical_candidate_source_versions
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE historical_candidate_source_versions
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
""" + CANDIDATE_LOOKUP_INDEX_SQL


def ensure_schema() -> None:
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured")
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(741852965)")
        conn.execute(DDL)
        conn.execute(
            """
            INSERT INTO historical_candidate_source_versions (
                version_key, generator_version, record_type, record_id,
                published_ts, observed_ts, status, reason, candidate_ids, processed_at
            )
            SELECT version_key, generator_version, record_type, record_id,
                   published_ts, observed_ts, status, reason, candidate_ids, processed_at
            FROM historical_candidate_sources
            ON CONFLICT (version_key, generator_version) DO NOTHING
            """
        )
        conn.execute(
            """
            WITH duplicate_ids AS (
                SELECT candidate.value AS candidate_id
                FROM historical_candidate_source_versions source
                CROSS JOIN LATERAL jsonb_array_elements_text(source.candidate_ids) candidate(value)
                WHERE source.generator_version = %(generator_version)s
                GROUP BY candidate.value
                HAVING COUNT(*) > 1
            )
            UPDATE historical_candidate_source_versions source
            SET status = 'error',
                reason = 'retry_snapshot_identity_collision',
                next_retry_at = NOW()
            WHERE source.generator_version = %(generator_version)s
              AND source.status <> 'error'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(source.candidate_ids) candidate(value)
                  JOIN duplicate_ids duplicate ON duplicate.candidate_id = candidate.value
              )
            """,
            {"generator_version": GENERATOR_VERSION},
        )


def list_pending_sources(
    *, maximum_lag_seconds: int, limit: int,
    generator_version: str = GENERATOR_VERSION,
) -> list[dict[str, Any]]:
    if generator_version not in GENERATOR_SPECS:
        raise ValueError(f"Unknown candidate generator version: {generator_version}")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT v.version_key, v.record_type, v.record_id, v.article_url,
                   v.published_ts, v.observed_ts, v.pipeline_version, v.result
            FROM news_enrichment_versions v
            LEFT JOIN historical_candidate_source_versions s
              ON s.version_key = v.version_key
             AND s.generator_version = %(generator_version)s
            WHERE (s.version_key IS NULL OR (
                    s.status = 'error' AND COALESCE(s.next_retry_at, NOW()) <= NOW()
                  ))
              AND v.published_ts > 0
              AND v.observed_ts >= v.published_ts
              AND v.observed_ts - v.published_ts <= %(maximum_lag)s
              AND v.result->>'relevance_label' = ANY(%(relevance)s)
              AND v.result->>'article_verification_status' = ANY(%(verification)s)
              AND COALESCE((v.result->>'entity_link_version')::integer, 0) >= 2
              AND jsonb_array_length(COALESCE(v.result->'related_tickers', '[]'::jsonb)) > 0
            ORDER BY (s.version_key IS NULL) DESC, v.observed_ts DESC, v.version_key
            LIMIT %(limit)s
            """,
            {
                "maximum_lag": max(60, int(maximum_lag_seconds)),
                "generator_version": generator_version,
                "relevance": list(ACCEPTED_RELEVANCE),
                "verification": list(ACCEPTED_VERIFICATION),
                "limit": max(1, min(int(limit), 100)),
            },
        ).fetchall()
    return [dict(row) for row in rows]


def _article(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row.get("result") or {})
    return {
        **result,
        "dedup_id": row["record_id"],
        "url": row.get("article_url") or result.get("resolved_article_url") or "",
        "published_ts": int(row["published_ts"]),
        "observed_ts": int(row["observed_ts"]),
        "pipeline_version": row.get("pipeline_version"),
        "enrichment_version_key": row["version_key"],
        "tickers": result.get("related_tickers") or [],
    }


def list_evidence_as_of(
    ticker: str, *, as_of_ts: int, lookback_seconds: int, maximum_lag_seconds: int,
    limit: int = 500,
) -> list[dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (record_type, record_id)
                   version_key, record_type, record_id, article_url, published_ts,
                   observed_ts, pipeline_version, result
            FROM news_enrichment_versions
            WHERE observed_ts <= %(as_of_ts)s
              AND published_ts BETWEEN %(start_ts)s AND %(as_of_ts)s
              AND observed_ts >= published_ts
              AND observed_ts - published_ts <= %(maximum_lag)s
              AND result->'related_tickers' ? %(ticker)s
              AND result->>'relevance_label' = ANY(%(relevance)s)
              AND result->>'article_verification_status' = ANY(%(verification)s)
              AND COALESCE((result->>'entity_link_version')::integer, 0) >= 2
            ORDER BY record_type, record_id, observed_ts DESC
            LIMIT %(limit)s
            """,
            {
                "ticker": str(ticker).upper(), "as_of_ts": int(as_of_ts),
                "start_ts": int(as_of_ts) - max(3600, int(lookback_seconds)),
                "maximum_lag": max(60, int(maximum_lag_seconds)),
                "relevance": list(ACCEPTED_RELEVANCE),
                "verification": list(ACCEPTED_VERIFICATION),
                "limit": max(1, min(int(limit), 2000)),
            },
        ).fetchall()
    return [_article(dict(row)) for row in rows]


def _attach_mentions(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_ids = sorted({
        str(event_id)
        for article in articles
        for event_id in (article.get("event_ids") or [])
        if str(event_id).isdigit()
    })
    mentions = persistent_store.list_gdelt_mentions_for_events(event_ids, limit=5000)
    by_event: dict[str, list[dict[str, Any]]] = {}
    for mention in mentions:
        by_event.setdefault(str(mention.get("event_id")), []).append(mention)
    return [
        {**article, "gdelt_mentions": [
            mention for event_id in (article.get("event_ids") or [])
            for mention in by_event.get(str(event_id), [])
        ]}
        for article in articles
    ]


def record_source_result(
    source: dict[str, Any], *, status: str, reason: str | None, candidate_ids: list[str],
    generator_version: str = GENERATOR_VERSION,
) -> None:
    if generator_version not in GENERATOR_SPECS:
        raise ValueError(f"Unknown candidate generator version: {generator_version}")
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO historical_candidate_source_versions (
                version_key, generator_version, record_type, record_id, published_ts,
                observed_ts, status, reason, candidate_ids
            ) VALUES (
                %(version_key)s, %(generator_version)s, %(record_type)s, %(record_id)s,
                %(published_ts)s, %(observed_ts)s, %(status)s, %(reason)s,
                %(candidate_ids)s
            ) ON CONFLICT (version_key, generator_version) DO UPDATE SET
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                candidate_ids = EXCLUDED.candidate_ids,
                processed_at = NOW(),
                attempt_count = CASE
                    WHEN EXCLUDED.status = 'error'
                    THEN historical_candidate_source_versions.attempt_count + 1
                    ELSE historical_candidate_source_versions.attempt_count
                END,
                next_retry_at = CASE
                    WHEN EXCLUDED.status = 'error' THEN NOW() + (
                        LEAST(
                            3600,
                            30 * POWER(
                                2, LEAST(historical_candidate_source_versions.attempt_count, 7)
                            )
                        ) * INTERVAL '1 second'
                    )
                    ELSE NULL
                END
            """,
            {**source, "status": status, "reason": reason,
             "candidate_ids": psycopg.types.json.Jsonb(candidate_ids),
             "generator_version": generator_version},
        )


def generate_from_source(
    source: dict[str, Any], *, lookback_seconds: int = 86_400,
    maximum_lag_seconds: int = 86_400,
    generator_version: str = GENERATOR_VERSION,
) -> list[dict[str, Any]]:
    try:
        generator_spec = GENERATOR_SPECS[generator_version]
    except KeyError as exc:
        raise ValueError(f"Unknown candidate generator version: {generator_version}") from exc
    result = source.get("result") or {}
    tickers = sorted({str(value).strip().upper() for value in result.get("related_tickers") or [] if value})
    candidates = []
    for ticker in tickers:
        evidence = list_evidence_as_of(
            ticker, as_of_ts=int(source["observed_ts"]),
            lookback_seconds=lookback_seconds,
            maximum_lag_seconds=maximum_lag_seconds,
        )
        evidence = _attach_mentions(evidence)
        observations = persistent_store.list_market_observations_as_of(
            ticker, as_of_ts=int(source["observed_ts"]),
            start_ts=int(source["observed_ts"]) - lookback_seconds,
            observation_type="minute_candle", limit=2000,
        )
        hype = build_news_hype_features(
            ticker, articles=evidence, as_of_ts=int(source["observed_ts"]),
            market_observations=observations,
            lookback_seconds=lookback_seconds,
            feature_version=generator_spec.feature_version,
        )
        latest_price = next(
            (float(row.get("close") or row.get("price")) for row in reversed(observations)
             if float(row.get("close") or row.get("price") or 0) > 0),
            None,
        )
        payload = {
            "symbol": ticker, "ts": int(source["observed_ts"]),
            "source": "historical_news_hype", "action": "research_candidate",
            "ranking_only": True, "entry_eligible": False,
            "live_activation_eligible": False,
            "candidate_generator_version": generator_spec.generator_version,
            "research_experiment_version": (
                WATCH_PRIORITY_EXPERIMENT
                if generator_version == WATCH_PRIORITY_GENERATOR_VERSION else None
            ),
            "snapshot_identity": (
                f"{source['version_key']}:{ticker}"
                if generator_version == GENERATOR_VERSION
                else f"{source['version_key']}:{ticker}:{generator_version}"
            ),
            "source_enrichment_version_key": source["version_key"],
            "source_record_type": source["record_type"],
            "source_published_ts": int(source["published_ts"]),
            "source_observed_ts": int(source["observed_ts"]),
            "evidence_enrichment_version_keys": [
                row["enrichment_version_key"] for row in evidence
            ],
            "semantic_signal": {"ticker": ticker, "hype": hype},
            "market_signal": {"price": latest_price},
        }
        candidates.append(record_feature_snapshot(
            ticker, payload, source="historical_news_hype", ts=int(source["observed_ts"])
        ))
    record_source_result(
        source, status="generated" if candidates else "skipped",
        reason=None if candidates else "no_validated_tickers",
        candidate_ids=[row["id"] for row in candidates],
        generator_version=generator_version,
    )
    return candidates


def status(generator_version: str = GENERATOR_VERSION) -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS count
               FROM historical_candidate_source_versions
               WHERE generator_version = %(generator_version)s GROUP BY status""",
            {"generator_version": generator_version},
        ).fetchall()
        candidate_count = conn.execute(
            """SELECT COUNT(DISTINCT candidate.value) AS count
               FROM historical_candidate_source_versions source
               CROSS JOIN LATERAL jsonb_array_elements_text(source.candidate_ids) candidate(value)
               WHERE source.generator_version = %(generator_version)s""",
            {"generator_version": generator_version},
        ).fetchone()
    return {
        "generator_version": generator_version,
        "feature_version": GENERATOR_SPECS[generator_version].feature_version,
        "sources": {row["status"]: int(row["count"]) for row in rows},
        "candidate_count": int(candidate_count["count"] if candidate_count else 0),
        "ranking_only": True,
        "live_activation_eligible": False,
    }
