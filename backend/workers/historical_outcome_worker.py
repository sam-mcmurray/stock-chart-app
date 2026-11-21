from __future__ import annotations

import json
import logging
import os
import time

import redis

from services.historical_outcome_evaluator import (
    ensure_schema, evaluate_candidate, list_pending_candidates,
    load_candidate_candles, record_outcome, status,
)


logger = logging.getLogger(__name__)
r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
HEARTBEAT_KEY = os.getenv("HISTORICAL_OUTCOME_HEARTBEAT_KEY", "agent:historical_outcomes:heartbeat")


def run_forever() -> None:
    ensure_schema()
    batch_size = max(1, min(int(os.getenv("HISTORICAL_OUTCOME_BATCH_SIZE", "10")), 100))
    interval = max(0.25, float(os.getenv("HISTORICAL_OUTCOME_INTERVAL_SECONDS", "1")))
    while True:
        counts = {"processed": 0, "stored": 0, "errors": 0}
        candidates = []
        try:
            candidates = list_pending_candidates(limit=batch_size)
            for candidate in candidates:
                try:
                    candles = load_candidate_candles(candidate)
                    outcome = evaluate_candidate(candidate, candles)
                    counts["processed"] += 1
                    counts["stored"] += int(record_outcome(outcome))
                except Exception:
                    counts["errors"] += 1
                    logger.exception("historical outcome evaluation failed for %s", candidate["id"])
            snapshot = status()
            state = (
                "running" if candidates
                else "waiting_for_price_import" if not snapshot["outcomes"]
                else "idle"
            )
            r.set(HEARTBEAT_KEY, json.dumps({
                "status": state if not counts["errors"] else "degraded",
                "ts": int(time.time()), **counts, **snapshot,
            }))
        except Exception as exc:
            logger.exception("historical outcome worker cycle failed")
            r.set(HEARTBEAT_KEY, json.dumps({
                "status": "error", "ts": int(time.time()), "error": str(exc),
            }))
        time.sleep(interval if candidates else max(interval, 10))


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
