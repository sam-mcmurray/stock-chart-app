from __future__ import annotations

import json
import logging
import os
import time

import redis

from services.historical_candidate_generator import (
    GENERATOR_SPECS, GENERATOR_VERSION, ensure_schema, generate_from_source,
    list_pending_sources, record_source_result, status,
)
from services.research_experiments import ensure_schema as ensure_experiment_schema


logger = logging.getLogger(__name__)
r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
HEARTBEAT_KEY = os.getenv("HISTORICAL_CANDIDATE_HEARTBEAT_KEY", "agent:historical_candidates:heartbeat")
ACTIVE_GENERATOR_VERSION = os.getenv(
    "HISTORICAL_CANDIDATE_GENERATOR_VERSION", GENERATOR_VERSION
).strip()


def run_forever() -> None:
    if ACTIVE_GENERATOR_VERSION not in GENERATOR_SPECS:
        raise RuntimeError(f"Unknown historical candidate generator: {ACTIVE_GENERATOR_VERSION}")
    ensure_schema()
    ensure_experiment_schema()
    batch_size = max(1, min(int(os.getenv("HISTORICAL_CANDIDATE_BATCH_SIZE", "5")), 25))
    interval = max(0.25, float(os.getenv("HISTORICAL_CANDIDATE_INTERVAL_SECONDS", "1")))
    maximum_lag = max(60, int(os.getenv("HISTORICAL_CANDIDATE_MAX_LAG_SECONDS", "86400")))
    while True:
        counts = {"processed": 0, "generated": 0, "errors": 0}
        sources = []
        try:
            sources = list_pending_sources(
                maximum_lag_seconds=maximum_lag, limit=batch_size,
                generator_version=ACTIVE_GENERATOR_VERSION,
            )
            for source in sources:
                try:
                    candidates = generate_from_source(
                        source, maximum_lag_seconds=maximum_lag,
                        generator_version=ACTIVE_GENERATOR_VERSION,
                    )
                    counts["processed"] += 1
                    counts["generated"] += len(candidates)
                except Exception as exc:
                    counts["errors"] += 1
                    logger.exception("historical candidate generation failed for %s", source["version_key"])
                    record_source_result(
                        source, status="error", reason=str(exc)[:500], candidate_ids=[],
                        generator_version=ACTIVE_GENERATOR_VERSION,
                    )
            r.set(HEARTBEAT_KEY, json.dumps({
                "status": "ok" if not counts["errors"] else "degraded",
                "ts": int(time.time()), **counts, **status(ACTIVE_GENERATOR_VERSION),
            }))
        except Exception as exc:
            logger.exception("historical candidate worker cycle failed")
            r.set(HEARTBEAT_KEY, json.dumps({"status": "error", "ts": int(time.time()), "error": str(exc)}))
        time.sleep(interval if sources else max(interval, 10))


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
