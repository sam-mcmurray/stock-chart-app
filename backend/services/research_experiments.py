from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from types import MappingProxyType
from typing import Any

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.getenv("DATABASE_URL")


@dataclass(frozen=True)
class ResearchExperimentSpec:
    experiment_version: str
    feature_version: str
    generator_version: str
    baseline_feature_version: str
    baseline_generator_version: str
    registered_at: int
    registered_at_iso: str
    hypothesis: str
    primary_metric: str
    secondary_metrics: tuple[str, ...]
    horizon_bars: int
    minimum_post_registration_live_received_sample: int
    status: str


WATCH_PRIORITY_EXPERIMENT = "news-hype-watch-priority-v2"
EXPERIMENT_SPECS = MappingProxyType({
    WATCH_PRIORITY_EXPERIMENT: ResearchExperimentSpec(
        experiment_version=WATCH_PRIORITY_EXPERIMENT,
        feature_version="news-hype-v2-watch-priority",
        generator_version="news-hype-candidate-generator-v2-watch-priority",
        baseline_feature_version="news-hype-v1",
        baseline_generator_version="news-hype-candidate-generator-v1",
        registered_at=1_789_756_100,
        registered_at_iso="2026-09-18T18:28:20Z",
        hypothesis=(
            "Among candidates with no positive long-direction score, attention can improve "
            "watch priority for later technical confirmation without promoting those candidates "
            "above any positive-direction candidate."
        ),
        primary_metric="mean_mfe_pct",
        secondary_metrics=("mean_excess_return_pct", "win_rate_pct", "mean_mae_pct"),
        horizon_bars=60,
        minimum_post_registration_live_received_sample=500,
        status="preregistered_research_only",
    ),
})


DDL = """
CREATE TABLE IF NOT EXISTS research_experiments (
    experiment_version TEXT PRIMARY KEY,
    specification JSONB NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _payload(spec: ResearchExperimentSpec) -> dict[str, Any]:
    value = asdict(spec)
    value["secondary_metrics"] = list(spec.secondary_metrics)
    return value


def ensure_schema() -> None:
    if not DATABASE_URL:
        raise RuntimeError("PostgreSQL is not configured")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute(DDL)
        for spec in EXPERIMENT_SPECS.values():
            expected = _payload(spec)
            conn.execute(
                """
                INSERT INTO research_experiments (
                    experiment_version, specification, registered_at
                ) VALUES (%(version)s, %(specification)s, to_timestamp(%(registered_at)s))
                ON CONFLICT (experiment_version) DO NOTHING
                """,
                {
                    "version": spec.experiment_version,
                    "specification": psycopg.types.json.Jsonb(expected),
                    "registered_at": spec.registered_at,
                },
            )
            stored = conn.execute(
                "SELECT specification FROM research_experiments WHERE experiment_version = %s",
                (spec.experiment_version,),
            ).fetchone()
            if not stored or json.dumps(stored["specification"], sort_keys=True) != json.dumps(expected, sort_keys=True):
                raise RuntimeError(
                    f"Stored research experiment {spec.experiment_version} differs from its immutable code specification"
                )


def list_experiments() -> list[dict[str, Any]]:
    ensure_schema()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT specification, registered_at, created_at
            FROM research_experiments ORDER BY registered_at, experiment_version
            """
        ).fetchall()
    return [
        {
            **dict(row["specification"]),
            "registered_at_database": row["registered_at"].isoformat(),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
