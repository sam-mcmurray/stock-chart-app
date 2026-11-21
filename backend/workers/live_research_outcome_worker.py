from __future__ import annotations

import json
import logging
import os
import time

import redis

from services.live_research_experiment import (
    ensure_schema, evaluate_live_outcome, list_outcome_candidates,
    load_live_benchmark_candles, load_live_candles, record_live_outcome, status,
)


logger = logging.getLogger(__name__)
r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
HEARTBEAT_KEY = os.getenv(
    "LIVE_RESEARCH_OUTCOME_HEARTBEAT_KEY", "agent:live_research_outcomes:heartbeat"
)
STREAM_HEARTBEAT_KEY = os.getenv(
    "SCHWAB_ACCOUNT_STREAM_HEARTBEAT_KEY", "schwab:account_stream:heartbeat"
)


def _publish_heartbeat(payload: dict) -> None:
    try:
        r.set(HEARTBEAT_KEY, json.dumps(payload))
    except redis.RedisError:
        logger.warning("Unable to publish live research outcome heartbeat", exc_info=True)


def _subscribed_symbols(*, now_ts: int | None = None) -> list[str]:
    try:
        payload = json.loads(r.get(STREAM_HEARTBEAT_KEY) or "{}")
    except (TypeError, json.JSONDecodeError, redis.RedisError):
        return []
    if int(payload.get("ts") or 0) < int(now_ts or time.time()) - 30:
        return []
    if payload.get("status") not in {"connected", "degraded"}:
        return []
    return list(dict.fromkeys(
        str(symbol or "").strip().upper()
        for symbol in (payload.get("symbols") or [])
        if str(symbol or "").strip()
    ))


def run_forever() -> None:
    ensure_schema()
    interval = max(5.0, float(os.getenv("LIVE_RESEARCH_OUTCOME_INTERVAL_SECONDS", "10")))
    batch_size = max(1, min(int(os.getenv("LIVE_RESEARCH_OUTCOME_BATCH_SIZE", "100")), 500))
    while True:
        counts = {
            "processed": 0, "complete": 0, "partial": 0,
            "pending": 0, "insufficient": 0, "errors": 0,
        }
        try:
            subscribed_symbols = _subscribed_symbols()
            candidates = list_outcome_candidates(
                limit=batch_size, priority_symbols=subscribed_symbols,
            )
            for candidate in candidates:
                try:
                    outcome = evaluate_live_outcome(
                        candidate,
                        load_live_candles(candidate),
                        benchmark_candles=load_live_benchmark_candles(candidate),
                    )
                    record_live_outcome(outcome)
                    counts["processed"] += 1
                    counts[outcome["status"]] += 1
                except Exception:
                    counts["errors"] += 1
                    logger.exception("live research outcome failed for %s", candidate["candidate_id"])
            _publish_heartbeat({
                "status": "degraded" if counts["errors"] else "ok",
                "ts": int(time.time()),
                "priority_symbol_count": len(subscribed_symbols),
                **counts, **status(),
            })
        except Exception as exc:
            logger.exception("live research outcome cycle failed")
            _publish_heartbeat({
                "status": "error", "ts": int(time.time()), "error": str(exc),
            })
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
