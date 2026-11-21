import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
import requests

from services import persistent_store
from services.article_graph import (
    list_historical_articles,
)
from services.calendar_relevance import PartialClassificationError, configured
from services.financial_news_gate import classify_articles
from services.calendar_relevance_queue import pending_counts
from services.news_article_resolver import resolve_article_batch


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PIPELINE_VERSION = os.getenv("HISTORICAL_ARTICLE_PIPELINE_VERSION", "v1")
CHECKPOINT_NAME = f"historical_article_llm:{PIPELINE_VERSION}"
HEARTBEAT_KEY = "news:historical:llm:heartbeat"
BATCH_SIZE = max(1, min(int(os.getenv("HISTORICAL_ARTICLE_LLM_BATCH_SIZE", "4")), 8))
RETRY_BATCH_SIZE = max(1, min(int(os.getenv("HISTORICAL_ARTICLE_RETRY_BATCH_SIZE", "1")), 8))
RESOLVE_PIPELINE_BATCH_SIZE = max(
    1, min(int(os.getenv("HISTORICAL_ARTICLE_RESOLVE_PIPELINE_BATCH_SIZE", "4")), 8)
)
POLL_SECONDS = max(10, int(os.getenv("HISTORICAL_ARTICLE_LLM_POLL_SECONDS", "60")))
OFF_HOURS_ONLY = os.getenv("HISTORICAL_ARTICLE_OFF_HOURS_ONLY", "true").lower() == "true"
OFF_HOURS_START = int(os.getenv("HISTORICAL_ARTICLE_OFF_HOURS_START", "19"))
OFF_HOURS_END = int(os.getenv("HISTORICAL_ARTICLE_OFF_HOURS_END", "7"))
TIMEZONE = ZoneInfo(os.getenv("HISTORICAL_ARTICLE_TIMEZONE", "America/Chicago"))
r = redis.from_url(REDIS_URL, decode_responses=True)


def _heartbeat(status: str, **extra) -> None:
    r.set(HEARTBEAT_KEY, json.dumps({
        "status": status,
        "ts": int(time.time()),
        "pipeline_version": PIPELINE_VERSION,
        **extra,
    }))


def _off_hours(now: datetime | None = None) -> bool:
    if not OFF_HOURS_ONLY:
        return True
    current = now or datetime.now(TIMEZONE)
    if current.weekday() >= 5:
        return True
    if OFF_HOURS_START > OFF_HOURS_END:
        return current.hour >= OFF_HOURS_START or current.hour < OFF_HOURS_END
    return OFF_HOURS_START <= current.hour < OFF_HOURS_END


def _article_item(row: dict) -> dict:
    url = str(row.get("url") or "").strip()
    provider = str(row.get("provider") or "")
    return {
        "id": hashlib.sha1(url.encode("utf-8")).hexdigest(),
        "url": url,
        "source_urls": row.get("source_urls") or [],
        "event_ids": row.get("event_ids") or [],
        "title": row.get("title") or url,
        "summary": row.get("summary") or "",
        "source": row.get("source") or "",
        "published_ts": int(row.get("published_ts") or 0),
        "symbols": [],
        "title_inferred": "gdelt historical" in provider.lower(),
        "_checkpoint_counted": bool(row.get("_checkpoint_counted")),
    }


def _failure_result(article: dict, error: str) -> dict:
    return {
        "relevance_label": "uncertain",
        "relevance_score": 0.0,
        "relevance_confidence": 0.0,
        "relevance_reason": f"Historical LLM processing failed: {error}"[:500],
        "relevance_source": "processing_failure",
        "relevance_model": "",
        "relevance_classified_at": int(time.time()),
        "display_headline": article.get("title") or "",
        "original_headline": article.get("original_title") or article.get("title") or "",
        "headline_verified": bool(article.get("content_verified")),
        "article_verification_status": (
            article.get("headline_resolution_source")
            if article.get("content_verified")
            else "unverified"
        ),
        "article_content_source": article.get("content_resolution_source"),
        "supporting_article_url": article.get("supporting_url"),
        "related_tickers": [],
        "related_companies": [],
        "company_relationships": [],
        "processing_error": error[:1000],
    }


def _provider_unavailable(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        return response is None or int(response.status_code) >= 500
    return False


def _classify_resilient(items: list[dict]) -> tuple[dict[str, dict], list[tuple[dict, str]]]:
    if not items:
        return {}, []
    try:
        results = classify_articles(items)
    except PartialClassificationError as exc:
        return exc.results, [(item, str(exc)) for item in items if item["id"] not in exc.results]
    except Exception as exc:
        if _provider_unavailable(exc):
            raise
        if len(items) == 1:
            return {}, [(items[0], str(exc))]
        midpoint = len(items) // 2
        left_results, left_failures = _classify_resilient(items[:midpoint])
        right_results, right_failures = _classify_resilient(items[midpoint:])
        return {**left_results, **right_results}, left_failures + right_failures
    missing = [item for item in items if item["id"] not in results]
    if not missing:
        return results, []
    if len(missing) == 1:
        if len(items) > 1:
            recovered, failures = _classify_resilient(missing)
            return {**results, **recovered}, failures
        return results, [(items[0], "Model response omitted the requested article ID")]
    midpoint = len(missing) // 2
    left_results, left_failures = _classify_resilient(missing[:midpoint])
    right_results, right_failures = _classify_resilient(missing[midpoint:])
    return {
        **results,
        **left_results,
        **right_results,
    }, left_failures + right_failures


def process_batch(rows: list[dict]) -> dict:
    started_at = time.monotonic()
    items = [_article_item(row) for row in rows if row.get("url")]
    saved_urls = persistent_store.existing_historical_article_enrichment_urls([item["url"] for item in items])
    recovered = [item for item in items if item["url"] in saved_urls]
    items = [item for item in items if item["url"] not in saved_urls]
    persistent_store.recover_historical_article_results([item["url"] for item in recovered])
    if items and persistent_store.historical_graph_backlog_full():
        raise RuntimeError("Historical graph delivery backlog full; inference deferred")
    groups = [
        items[index:index + RESOLVE_PIPELINE_BATCH_SIZE]
        for index in range(0, len(items), RESOLVE_PIPELINE_BATCH_SIZE)
    ]
    processed = len(recovered)
    failed = 0
    completed_delta = sum(not item.get("_checkpoint_counted") for item in recovered)
    scanned = len(recovered)
    resolve_wait_seconds = inference_seconds = persist_seconds = 0.0

    def resolve(group: list[dict]) -> list[dict]:
        return resolve_article_batch(group, workers=min(4, len(group)))

    if groups:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(resolve, groups[0])
            for index in range(len(groups)):
                wait_started = time.monotonic()
                resolved = future.result()
                resolve_wait_seconds += time.monotonic() - wait_started
                if index + 1 < len(groups):
                    future = executor.submit(resolve, groups[index + 1])

                inference_started = time.monotonic()
                try:
                    results, classification_failures = _classify_resilient(resolved)
                except Exception as exc:
                    logger.warning("Historical article classification batch failed: %s", exc)
                    results = {}
                    classification_failures = [
                        (article, str(exc)) for article in resolved
                    ]
                inference_seconds += time.monotonic() - inference_started
                failure_by_id = {
                    article["id"]: error
                    for article, error in classification_failures
                }

                persist_started = time.monotonic()
                successful = []
                for article in resolved:
                    result = results.get(article["id"])
                    if not result or result.get("relevance_source") == "processing_failure":
                        persistent_store.enqueue_historical_article_retry(
                            article,
                            PIPELINE_VERSION,
                            failure_by_id.get(article["id"])
                            or (result or {}).get("processing_error")
                            or "Model processing failure",
                        )
                        failed += 1
                        continue
                    successful.append((article, result))
                if successful:
                    persistent_store.save_historical_article_results(successful, PIPELINE_VERSION)
                for article, result in successful:
                    processed += 1
                    completed_delta += int(not article.get("_checkpoint_counted"))
                persist_seconds += time.monotonic() - persist_started
                scanned += len(resolved)

    duration_seconds = max(0.001, time.monotonic() - started_at)
    return {
        "scanned": scanned,
        "processed": processed,
        "failed": failed,
        "completed_delta": completed_delta,
        "reused_results": len(recovered),
        "duration_seconds": round(duration_seconds, 3),
        "resolve_seconds": round(resolve_wait_seconds, 3),
        "inference_seconds": round(inference_seconds, 3),
        "persist_seconds": round(persist_seconds, 3),
        "pipelined_groups": len(groups),
        "records_per_minute": round(scanned * 60 / duration_seconds, 2),
    }


def run_forever() -> None:
    persistent_store.ensure_schema()
    checkpoint = persistent_store.load_worker_checkpoint(CHECKPOINT_NAME)
    after_url = str(checkpoint.get("after_url") or "")
    completed = int(checkpoint.get("completed") or 0)
    while True:
        if not configured():
            _heartbeat("waiting_for_model", after_url=after_url, completed=completed)
            time.sleep(POLL_SECONDS)
            continue
        if not _off_hours():
            _heartbeat("waiting_for_off_hours", after_url=after_url, completed=completed)
            time.sleep(POLL_SECONDS)
            continue
        current_counts = pending_counts(r)
        current_db_work = persistent_store.count_market_news_relevance_work()
        rows = list_historical_articles(after_url=after_url, limit=BATCH_SIZE)
        if not rows:
            _heartbeat("complete", after_url=after_url, completed=completed)
            time.sleep(max(POLL_SECONDS, 3600))
            continue
        _heartbeat(
            "processing",
            after_url=after_url,
            completed=completed,
            batch_size=len(rows),
            current_url=rows[0].get("url"),
            current_live_queue=current_counts["live"],
            current_backlog_queue=current_counts["backlog"],
            current_db_work=current_db_work,
        )
        try:
            stats = process_batch(rows)
        except Exception as exc:
            logger.exception("Historical article batch failed")
            _heartbeat("error", after_url=after_url, completed=completed, error=str(exc)[:500])
            time.sleep(POLL_SECONDS)
            continue
        after_url = str(rows[-1]["url"])
        completed += stats["processed"]
        persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, {
            "after_url": after_url,
            "completed": completed,
            "pipeline_version": PIPELINE_VERSION,
        })
        _heartbeat(
            "ok",
            after_url=after_url,
            completed=completed,
            batch_size=len(rows),
            failed=stats["failed"],
            current_live_queue=current_counts["live"],
            current_backlog_queue=current_counts["backlog"],
            current_db_work=current_db_work,
        )


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting historical article LLM worker")
    run_forever()
