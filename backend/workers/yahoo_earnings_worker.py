import argparse
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import redis

from news_sources.yahoo_earnings_source import (
    PAGE_SIZE,
    YahooEarningsRateLimited,
    calendar_event,
    load_earnings_page,
)
from services import persistent_store


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
START_DATE = os.getenv("EARNINGS_YAHOO_BACKFILL_START_DATE", "2000-01-01")
END_DATE = os.getenv("EARNINGS_YAHOO_BACKFILL_END_DATE", "")
REQUEST_INTERVAL_SECONDS = max(5, int(os.getenv("EARNINGS_YAHOO_REQUEST_INTERVAL_SECONDS", "60")))
RATE_LIMIT_BACKOFF_SECONDS = max(300, int(os.getenv("EARNINGS_YAHOO_RATE_LIMIT_BACKOFF_SECONDS", "21600")))
ERROR_BACKOFF_SECONDS = max(60, int(os.getenv("EARNINGS_YAHOO_ERROR_BACKOFF_SECONDS", "3600")))
OFF_HOURS_ONLY = os.getenv("EARNINGS_YAHOO_OFF_HOURS_ONLY", "true").lower() == "true"
OFF_HOURS_START = int(os.getenv("EARNINGS_YAHOO_OFF_HOURS_START", "19"))
OFF_HOURS_END = int(os.getenv("EARNINGS_YAHOO_OFF_HOURS_END", "7"))
TIMEZONE = ZoneInfo(os.getenv("EARNINGS_YAHOO_TIMEZONE", "America/Chicago"))
K_CURSOR = "calendar:earnings:yahoo:cursor"
K_OFFSET = "calendar:earnings:yahoo:offset"
K_COMPLETE = "calendar:earnings:yahoo:complete"
K_LAST_REQUEST = "calendar:earnings:yahoo:last_request"
K_HEARTBEAT = "calendar:earnings:yahoo:heartbeat"
K_NEWS_MIGRATED = "calendar:earnings:news_reclassified:v1"
r = redis.from_url(REDIS_URL, decode_responses=True)


def _heartbeat(status: str, **extra):
    try:
        r.set(K_HEARTBEAT, json.dumps({"status": status, "ts": int(time.time()), **extra}))
    except Exception as exc:
        logger.warning("Unable to publish Yahoo earnings heartbeat: %s", exc)


def _off_hours(now: datetime | None = None) -> bool:
    if not OFF_HOURS_ONLY:
        return True
    current = now or datetime.now(TIMEZONE)
    if current.weekday() >= 5:
        return True
    if OFF_HOURS_START > OFF_HOURS_END:
        return current.hour >= OFF_HOURS_START or current.hour < OFF_HOURS_END
    return OFF_HOURS_START <= current.hour < OFF_HOURS_END


def _request_gate(sleep=time.sleep):
    last_request = int(r.get(K_LAST_REQUEST) or 0)
    remaining = REQUEST_INTERVAL_SECONDS - (int(time.time()) - last_request)
    if remaining > 0:
        sleep(remaining)
    r.set(K_LAST_REQUEST, int(time.time()))


def sync_day(
    day: date,
    *,
    start_offset: int = 0,
    max_pages: int | None = None,
    request_gate=_request_gate,
    checkpoint_offset: bool = True,
) -> dict:
    offset = max(0, int(start_offset))
    pages = records = 0
    total = None
    while total is None or offset < total:
        request_gate()
        rows, total = load_earnings_page(day, offset=offset, size=PAGE_SIZE)
        persistent_store.upsert_earnings_events(rows)
        persistent_store.upsert_calendar_events([calendar_event(row) for row in rows])
        records += len(rows)
        pages += 1
        offset += PAGE_SIZE
        if checkpoint_offset:
            r.set(K_OFFSET, offset)
        _heartbeat(
            "backfilling",
            cursor=day.isoformat(),
            offset=offset,
            pages=pages,
            records=records,
            day_total=total,
        )
        if max_pages is not None and pages >= max_pages:
            break
        if total == 0:
            break
    complete = total is not None and offset >= total
    return {
        "date": day.isoformat(),
        "pages": pages,
        "records": records,
        "day_total": int(total or 0),
        "next_offset": offset,
        "complete": complete,
    }


def _cursor() -> date:
    value = r.get(K_CURSOR) or END_DATE or date.today().isoformat()
    return datetime.strptime(value, "%Y-%m-%d").date()


def _progress() -> dict:
    try:
        return {
            "cursor": _cursor().isoformat(),
            "offset": int(r.get(K_OFFSET) or 0),
        }
    except Exception:
        return {"cursor": None, "offset": 0}


def backfill_once() -> dict:
    cursor = _cursor()
    start = datetime.strptime(START_DATE, "%Y-%m-%d").date()
    if cursor < start:
        r.set(K_COMPLETE, "1")
        result = {"complete": True, "start_date": START_DATE}
        _heartbeat("complete", **result)
        return result
    if cursor.weekday() >= 5:
        next_day = cursor - timedelta(days=1)
        r.set(K_CURSOR, next_day.isoformat())
        r.delete(K_OFFSET)
        result = {
            "date": cursor.isoformat(),
            "pages": 0,
            "records": 0,
            "day_total": 0,
            "next_offset": 0,
            "complete": True,
            "skipped": "weekend",
        }
        _heartbeat("backfilling", cursor=cursor.isoformat(), next_cursor=next_day.isoformat(), **result)
        return result
    offset = max(0, int(r.get(K_OFFSET) or 0))
    result = sync_day(cursor, start_offset=offset)
    if result["complete"]:
        next_day = cursor - timedelta(days=1)
        r.set(K_CURSOR, next_day.isoformat())
        r.delete(K_OFFSET)
    return result


def _reclassify_news_events():
    if r.get(K_NEWS_MIGRATED) == "1":
        return
    changed = persistent_store.reclassify_news_earnings_events()
    r.set(K_NEWS_MIGRATED, "1")
    logger.info("Reclassified %s news-derived earnings events as market notes", changed)


def run_forever():
    migration_complete = False
    while True:
        try:
            if not migration_complete:
                _reclassify_news_events()
                migration_complete = True
            if r.get(K_COMPLETE) == "1":
                _heartbeat("complete", start_date=START_DATE, cursor=r.get(K_CURSOR))
                time.sleep(86400)
                continue
            if not _off_hours():
                _heartbeat("waiting_for_off_hours", cursor=_cursor().isoformat(), offset=int(r.get(K_OFFSET) or 0))
                time.sleep(300)
                continue
            result = backfill_once()
            logger.info("Yahoo earnings backfill day complete: %s", result)
        except YahooEarningsRateLimited as exc:
            _heartbeat(
                "rate_limited",
                **_progress(),
                retry_seconds=RATE_LIMIT_BACKOFF_SECONDS,
                error=str(exc),
            )
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
        except Exception as exc:
            logger.exception("Yahoo earnings backfill failed")
            _heartbeat(
                "error",
                **_progress(),
                retry_seconds=ERROR_BACKOFF_SECONDS,
                error=str(exc)[:500],
            )
            time.sleep(ERROR_BACKOFF_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="Slow, resumable Yahoo earnings-calendar importer")
    parser.add_argument("--day", help="Import one YYYY-MM-DD day without changing the historical cursor")
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args()
    if args.day:
        _reclassify_news_events()
        selected = datetime.strptime(args.day, "%Y-%m-%d").date()
        print(json.dumps(sync_day(
            selected,
            max_pages=args.max_pages,
            checkpoint_offset=False,
        ), indent=2))
        return
    run_forever()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting Yahoo earnings worker")
    main()
