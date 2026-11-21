from __future__ import annotations

import json
import logging
import os
import time

import redis

from routers.auth import get_market_data_access_token
from schwab.market_data import SchwabMarketDataClient
from schwab.user_preferences import UserPreferences
from services.historical_price_import import (
    ensure_schema, mark_failed, mark_started, next_import, plan_candidate_imports,
    plan_benchmark_imports, recover_interrupted_imports, status, store_result,
)
from services.decision_engine import market_session


logger = logging.getLogger(__name__)
r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
HEARTBEAT_KEY = os.getenv("HISTORICAL_PRICE_IMPORT_HEARTBEAT_KEY", "agent:historical_price_import:heartbeat")


def run_forever() -> None:
    ensure_schema()
    recovered = recover_interrupted_imports()
    if recovered:
        logger.warning("Recovered %s interrupted historical price import(s)", recovered)
    planned = plan_candidate_imports() + plan_benchmark_imports()
    interval = max(1.0, float(os.getenv("HISTORICAL_PRICE_IMPORT_INTERVAL_SECONDS", "1")))
    off_hours_only = os.getenv("HISTORICAL_PRICE_IMPORT_OFF_HOURS_ONLY", "true").lower() == "true"
    while True:
        task = next_import()
        if not task:
            newly_planned = plan_candidate_imports() + plan_benchmark_imports()
            planned += newly_planned
            if newly_planned:
                continue
            r.set(HEARTBEAT_KEY, json.dumps({
                "status": "complete", "ts": int(time.time()), "planned_total": planned,
                **status(include_coverage=False),
            }))
            time.sleep(30)
            continue
        session = market_session()
        if off_hours_only and session["is_regular"]:
            r.set(HEARTBEAT_KEY, json.dumps({
                "status": "waiting", "ts": int(time.time()),
                "reason": "regular_market_session", "next_symbol": task["symbol"],
                **status(include_coverage=False),
            }))
            time.sleep(60)
            continue
        try:
            token = get_market_data_access_token()
        except Exception as exc:
            r.set(HEARTBEAT_KEY, json.dumps({
                "status": "idle", "ts": int(time.time()), "reason": "schwab_auth_required",
                "detail": str(exc), **status(include_coverage=False),
            }))
            time.sleep(10)
            continue
        mark_started(task["import_key"])
        try:
            client = SchwabMarketDataClient(UserPreferences(token), access_token=token)
            payload = client.get_price_history(
                task["symbol"], frequencyType="minute",
                frequency=int(task["frequency_minutes"]), needExtendedHoursData=True,
                startDate=int(task["start_ts"]) * 1000,
                endDate=int(task["end_ts"]) * 1000,
            )
            result = store_result(task, payload, received_ts=int(time.time()))
            r.set(HEARTBEAT_KEY, json.dumps({
                "status": "running", "ts": int(time.time()), "symbol": task["symbol"],
                **result, **status(include_coverage=False),
            }))
        except Exception as exc:
            logger.exception("historical price import failed for %s", task["symbol"])
            mark_failed(task, str(exc))
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
