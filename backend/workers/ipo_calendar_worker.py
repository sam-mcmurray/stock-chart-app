import json
import logging
import os
import time
from datetime import datetime, timezone

import redis

from news_sources.ipo_scoop_source import load_ipo_buzz_page, load_ipo_calendar, load_ipo_pricings
from services import persistent_store


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
SYNC_INTERVAL_SECONDS = int(os.getenv("IPO_SCOOP_SYNC_INTERVAL_SECONDS", "86400"))
REQUEST_DELAY_SECONDS = float(os.getenv("IPO_SCOOP_REQUEST_DELAY_SECONDS", "0.5"))
ERROR_BACKOFF_SECONDS = max(30, int(os.getenv("IPO_SCOOP_ERROR_BACKOFF_SECONDS", "300")))
BUZZ_START_YEAR = int(os.getenv("IPO_SCOOP_BUZZ_START_YEAR", "2006"))
K_BUZZ_PAGE = "calendar:iposcoop:buzz_page"
K_BUZZ_COMPLETE = "calendar:iposcoop:buzz_complete"
K_HEARTBEAT = "calendar:iposcoop:heartbeat"
r = redis.from_url(REDIS_URL, decode_responses=True)


def _heartbeat(status: str, **extra):
    try:
        r.set(K_HEARTBEAT, json.dumps({"status": status, "ts": int(time.time()), **extra}))
    except Exception as exc:
        logger.warning("Unable to publish IPO Scoop heartbeat: %s", exc)


def _sync_buzz() -> int:
    imported = 0
    if r.get(K_BUZZ_COMPLETE) == "1":
        events, _ = load_ipo_buzz_page(page=1, per_page=100, order="desc")
        return persistent_store.upsert_calendar_events(events)

    page = max(1, int(r.get(K_BUZZ_PAGE) or 1))
    while True:
        events, total_pages = load_ipo_buzz_page(page=page, per_page=100, order="asc")
        events = [event for event in events if int(event["date"][:4]) >= BUZZ_START_YEAR]
        imported += persistent_store.upsert_calendar_events(events)
        if page >= total_pages:
            r.set(K_BUZZ_COMPLETE, 1)
            r.delete(K_BUZZ_PAGE)
            break
        page += 1
        r.set(K_BUZZ_PAGE, page)
        time.sleep(max(0, REQUEST_DELAY_SECONDS))
    return imported


def sync_once() -> dict:
    started = int(time.time())
    year = datetime.now(timezone.utc).year
    current_events = load_ipo_calendar()
    current = persistent_store.upsert_calendar_events(current_events)
    removed = persistent_store.prune_calendar_events(
        "ipo_scoop_calendar",
        [event["id"] for event in current_events],
    )
    priced = 0
    for pricing_year in range(max(2024, year - 2), year + 1):
        priced += persistent_store.upsert_calendar_events(load_ipo_pricings(pricing_year))
        time.sleep(max(0, REQUEST_DELAY_SECONDS))
    buzz = _sync_buzz()
    result = {"started_ts": started, "current": current, "current_removed": removed, "priced": priced, "buzz": buzz}
    _heartbeat("ok", **result)
    return result


def run_forever():
    while True:
        sleep_seconds = max(300, SYNC_INTERVAL_SECONDS)
        try:
            result = sync_once()
            logger.info("IPO Scoop sync complete: %s", result)
        except Exception as exc:
            logger.exception("IPO Scoop sync failed")
            _heartbeat("error", error=str(exc))
            sleep_seconds = ERROR_BACKOFF_SECONDS
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting IPO Scoop calendar worker")
    run_forever()
