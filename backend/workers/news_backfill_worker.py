import json
import logging
import os
import sys
import time
from collections import deque

import redis

from services import persistent_store
from services.active_news_store import remove_unrelated_article
from services.article_graph import (
    ensure_historical_article_index,
    historical_article_partition_size,
    list_historical_articles_newest,
    newest_historical_article_timestamp,
)
from services.calendar_relevance import configured, should_remove
from services.calendar_relevance_queue import (
    CLASSIFIED_KEY,
    ENRICHMENTS_KEY,
    QUEUED_KEY,
    REDIS_URL,
    pending_counts,
)
from services.news_article_resolver import resolve_article_batch
from workers.calendar_relevance_worker import _classify_resilient
from workers.historical_article_worker import PIPELINE_VERSION, process_batch


logger = logging.getLogger(__name__)
CHECKPOINT_SUFFIX = os.getenv("NEWS_BACKFILL_CHECKPOINT_SUFFIX", "").strip()
CHECKPOINT_TAG = f":{CHECKPOINT_SUFFIX}" if CHECKPOINT_SUFFIX else ""
CHECKPOINT_NAME = f"news_backfill:{PIPELINE_VERSION}{CHECKPOINT_TAG}"
HISTORICAL_CHECKPOINT_NAME = f"historical_article_llm:{PIPELINE_VERSION}{CHECKPOINT_TAG}"
HEARTBEAT_KEY = os.getenv("NEWS_BACKFILL_HEARTBEAT_KEY", "news:historical:llm:heartbeat")
BATCH_SIZE = max(1, min(int(os.getenv("NEWS_BACKFILL_BATCH_SIZE", "8")), 8))
RETRY_BATCH_SIZE = max(1, min(int(os.getenv("HISTORICAL_ARTICLE_RETRY_BATCH_SIZE", "1")), 4))
RETRY_EVERY_BATCHES = max(1, int(os.getenv("HISTORICAL_ARTICLE_RETRY_EVERY_BATCHES", "10")))
MODEL_WAIT_SECONDS = max(10, int(os.getenv("NEWS_BACKFILL_MODEL_WAIT_SECONDS", "60")))
START_BEFORE_PUBLISHED_TS = max(0, int(os.getenv("NEWS_BACKFILL_START_BEFORE_PUBLISHED_TS", "0")))
MIN_PUBLISHED_TS = max(0, int(os.getenv("NEWS_BACKFILL_MIN_PUBLISHED_TS", "0")))
SKIP_CALENDAR_BACKLOG = os.getenv("NEWS_BACKFILL_SKIP_CALENDAR_BACKLOG", "false").lower() == "true"
PROCESS_RETRIES = os.getenv("NEWS_BACKFILL_PROCESS_RETRIES", "true").lower() == "true"
YIELD_TO_LIVE_QUEUE = os.getenv("NEWS_BACKFILL_YIELD_TO_LIVE_QUEUE", "false").lower() == "true"
LIVE_QUEUE_YIELD_SECONDS = max(2, int(os.getenv("NEWS_BACKFILL_LIVE_QUEUE_YIELD_SECONDS", "5")))
r = redis.from_url(REDIS_URL, decode_responses=True)


class _ForwardProgressRate:
    """Cursor throughput including intervening retry/live-queue wall time."""

    def __init__(self):
        self.samples = deque(maxlen=20)
        self.last_finished = None

    def record(self, rows: int, started: float, finished: float) -> float:
        elapsed = finished - (self.last_finished if self.last_finished is not None else started)
        if rows <= 0 or elapsed <= 0:
            return self.rate
        self.samples.append((rows, elapsed))
        self.last_finished = finished
        return self.rate

    @property
    def rate(self) -> float:
        seconds = sum(seconds for _, seconds in self.samples)
        return 60 * sum(rows for rows, _ in self.samples) / seconds if seconds else 0.0


_last_forward_batch = None


def _heartbeat(status: str, **extra) -> None:
    global _last_forward_batch
    now = int(time.time())
    if status == "historical_articles_ok":
        _last_forward_batch = {"completed_at": now, **{key: extra[key] for key in (
            "batch_size", "failed", "skipped_existing", "selection_seconds", "existing_lookup_seconds",
            "checkpoint_seconds", "forward_seconds", "duration_seconds", "resolve_seconds",
            "inference_seconds", "persist_seconds",
        ) if key in extra}}
    r.set(HEARTBEAT_KEY, json.dumps({
        "status": status,
        "ts": now,
        "pipeline_version": PIPELINE_VERSION,
        "last_forward_batch": _last_forward_batch,
        **extra,
    }))


def _historical_progress(
    total: int,
    remaining: int,
    rate: float = 0,
    cursor_published_ts: int = 0,
) -> dict:
    remaining = max(0, int(remaining))
    return {
        "historical_total": max(0, int(total)),
        "historical_remaining": remaining,
        "historical_eta_seconds": round(remaining * 60 / rate) if rate > 0 else None,
        "historical_cursor_published_ts": max(0, int(cursor_published_ts or 0)),
        "historical_rate_per_minute": round(max(0.0, float(rate)), 2),
    }


def _calendar_item(event: dict) -> dict:
    details = event.get("details") or {}
    return {
        "id": str(event.get("id") or ""),
        "title": event.get("title") or "",
        "summary": details.get("summary") or "",
        "source": event.get("source") or "",
        "symbols": event.get("symbols") or [],
        "url": event.get("url") or "",
        "source_urls": details.get("source_urls") or [],
        "event_ids": details.get("event_ids") or [],
        "title_inferred": bool(details.get("title_inferred")),
        "published_ts": int(details.get("published_ts") or 0),
    }


def _failure_result(article: dict, error: str) -> dict:
    return {
        "relevance_label": "uncertain",
        "relevance_score": 0.0,
        "relevance_confidence": 0.0,
        "relevance_reason": f"Backfill LLM processing failed: {error}"[:500],
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


def process_calendar_batch(rows: list[dict]) -> dict:
    items = [_calendar_item(row) for row in rows if row.get("id")]
    resolved = resolve_article_batch(items)
    results, failures = _classify_resilient(resolved)
    failure_by_id = {item["id"]: error for item, error in failures}
    processed = removed = failed = 0
    for article in resolved:
        event_id = article["id"]
        result = results.get(event_id)
        if not result:
            result = _failure_result(
                article,
                failure_by_id.get(event_id, "Model omitted the requested article"),
            )
            failed += 1
        r.hset(ENRICHMENTS_KEY, event_id, json.dumps(result))
        if should_remove(result):
            removed += int(persistent_store.delete_calendar_event(event_id))
            remove_unrelated_article(r, article)
        else:
            persistent_store.update_calendar_event_relevance(event_id, result)
        r.sadd(CLASSIFIED_KEY, event_id)
        r.srem(QUEUED_KEY, event_id)
        processed += 1
    return {"processed": processed, "removed": removed, "failed": failed}


def run() -> None:
    while True:
        try:
            persistent_store.ensure_schema()
            break
        except Exception as exc:
            logger.warning("PostgreSQL unavailable; waiting to resume backfill: %s", exc)
            _heartbeat("waiting_for_database")
            time.sleep(MODEL_WAIT_SECONDS)
    if os.getenv("NEWS_BACKFILL_SHARED_QUEUE", "false").lower() == "true":
        from workers.shared_news_backfill import run as run_shared
        return run_shared(sys.modules[__name__])
    checkpoint = persistent_store.load_worker_checkpoint(CHECKPOINT_NAME)
    if checkpoint.get("phase") == "complete":
        _heartbeat(
            "complete",
            calendar_completed=int(checkpoint.get("calendar_completed") or 0),
            historical_completed=int(checkpoint.get("historical_completed") or 0),
        )
        return

    cutoff_ts = int(checkpoint.get("cutoff_ts") or time.time())
    calendar_after_id = str(checkpoint.get("calendar_after_id") or "")
    calendar_completed = int(checkpoint.get("calendar_completed") or 0)
    historical_checkpoint = persistent_store.load_worker_checkpoint(
        HISTORICAL_CHECKPOINT_NAME
    )
    newest_first_v2 = bool(checkpoint.get("newest_first_v2"))
    historical_before_published_ts = int(
        checkpoint.get("historical_before_published_ts") or 0
    )
    historical_before_url = str(checkpoint.get("historical_before_url") or "")
    historical_start_published_ts = int(
        checkpoint.get("historical_start_published_ts") or 0
    )
    if not newest_first_v2:
        historical_start_published_ts = (
            START_BEFORE_PUBLISHED_TS or newest_historical_article_timestamp()
        )
        historical_before_published_ts = historical_start_published_ts + (
            0 if START_BEFORE_PUBLISHED_TS else 1
        )
        historical_before_url = ""
        newest_first_v2 = True
    historical_completed = int(
        checkpoint.get("historical_completed")
        or historical_checkpoint.get("completed")
        or 0
    )
    phase = str(checkpoint.get("phase") or (
        "historical_articles" if SKIP_CALENDAR_BACKLOG else "calendar_backlog"
    ))
    historical_remaining = historical_article_partition_size(
        min_published_ts=MIN_PUBLISHED_TS,
        before_published_ts=historical_before_published_ts,
        before_url=historical_before_url,
    )
    historical_rate = 0.0
    forward_rate = _ForwardProgressRate()
    seeded_retries = (
        persistent_store.seed_historical_article_retries(PIPELINE_VERSION)
        if PROCESS_RETRIES else 0
    )
    retry_accounting_v2 = bool(checkpoint.get("retry_accounting_v2"))
    if PROCESS_RETRIES and not retry_accounting_v2:
        pending_retries = persistent_store.count_historical_article_retries()
        historical_completed = max(0, historical_completed - pending_retries)
        retry_accounting_v2 = True
        checkpoint["retry_accounting_v2"] = True
        checkpoint["historical_completed"] = historical_completed
        checkpoint["newest_first_v2"] = newest_first_v2
        checkpoint["historical_before_published_ts"] = historical_before_published_ts
        checkpoint["historical_before_url"] = historical_before_url
        checkpoint["historical_start_published_ts"] = historical_start_published_ts
        persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, checkpoint)
        logger.info(
            "Queued %s legacy failures; corrected completed count by %s",
            seeded_retries,
            pending_retries,
        )
    elif not PROCESS_RETRIES:
        retry_accounting_v2 = True
    historical_total = (
        historical_completed
        + historical_remaining
        + (persistent_store.count_historical_article_retries() if PROCESS_RETRIES else 0)
    )

    while not configured():
        _heartbeat(
            "waiting_for_model",
            phase=phase,
            calendar_completed=calendar_completed,
            historical_completed=historical_completed,
        )
        time.sleep(MODEL_WAIT_SECONDS)

    while phase == "calendar_backlog":
        rows = persistent_store.list_market_news_backfill_page(
            cutoff_ts=cutoff_ts,
            after_id=calendar_after_id,
            limit=BATCH_SIZE,
        )
        if not rows:
            phase = "historical_articles"
            persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, {
                "phase": phase,
                "cutoff_ts": cutoff_ts,
                "calendar_after_id": calendar_after_id,
                "calendar_completed": calendar_completed,
                "historical_before_published_ts": historical_before_published_ts,
                "historical_before_url": historical_before_url,
                "historical_start_published_ts": historical_start_published_ts,
                "historical_completed": historical_completed,
                "retry_accounting_v2": retry_accounting_v2,
                "newest_first_v2": newest_first_v2,
            })
            break
        _heartbeat(
            "processing_calendar_backlog",
            phase=phase,
            calendar_completed=calendar_completed,
            historical_completed=historical_completed,
            batch_size=len(rows),
            current_id=rows[0]["id"],
        )
        stats = process_calendar_batch(rows)
        calendar_after_id = str(rows[-1]["id"])
        calendar_completed += stats["processed"]
        persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, {
            "phase": phase,
            "cutoff_ts": cutoff_ts,
            "calendar_after_id": calendar_after_id,
            "calendar_completed": calendar_completed,
            "historical_before_published_ts": historical_before_published_ts,
            "historical_before_url": historical_before_url,
            "historical_start_published_ts": historical_start_published_ts,
            "historical_completed": historical_completed,
        })
        _heartbeat(
            "calendar_backlog_ok",
            phase=phase,
            calendar_completed=calendar_completed,
            historical_completed=historical_completed,
            batch_size=len(rows),
            removed=stats["removed"],
            failed=stats["failed"],
        )

    ensure_historical_article_index()
    historical_batches_since_retry = 0
    while phase == "historical_articles":
        if YIELD_TO_LIVE_QUEUE:
            live_counts = pending_counts(r)
            if live_counts["total"]:
                _heartbeat(
                    "waiting_for_live_queue",
                    phase=phase,
                    historical_completed=historical_completed,
                    live_queue=live_counts,
                    **_historical_progress(
                        historical_total,
                        historical_remaining,
                        historical_rate,
                        historical_before_published_ts,
                    ),
                )
                time.sleep(LIVE_QUEUE_YIELD_SECONDS)
                continue
        retry_rows = (
            persistent_store.list_historical_article_retries(limit=RETRY_BATCH_SIZE)
            if PROCESS_RETRIES and historical_batches_since_retry >= RETRY_EVERY_BATCHES
            else []
        )
        if retry_rows:
            retry_count = persistent_store.count_historical_article_retries()
            _heartbeat(
                "retrying_historical_articles",
                phase=phase,
                calendar_completed=calendar_completed,
                historical_completed=historical_completed,
                retry_pending=retry_count,
                batch_size=len(retry_rows),
                current_url=retry_rows[0].get("url"),
            )
            stats = process_batch(retry_rows)
            historical_completed += stats.get("completed_delta", 0)
            persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, {
                "phase": phase,
                "cutoff_ts": cutoff_ts,
                "calendar_after_id": calendar_after_id,
                "calendar_completed": calendar_completed,
                "historical_before_published_ts": historical_before_published_ts,
                "historical_before_url": historical_before_url,
                "historical_start_published_ts": historical_start_published_ts,
                "historical_completed": historical_completed,
                "retry_accounting_v2": retry_accounting_v2,
                "newest_first_v2": newest_first_v2,
            })
            _heartbeat(
                "historical_retry_ok",
                phase=phase,
                calendar_completed=calendar_completed,
                historical_completed=historical_completed,
                retry_pending=persistent_store.count_historical_article_retries(),
                **{key: stats[key] for key in (
                    "duration_seconds", "resolve_seconds", "inference_seconds",
                    "persist_seconds",
                ) if key in stats},
                retry_records_per_minute=float(stats.get("records_per_minute") or 0),
                **_historical_progress(
                    historical_total,
                    historical_remaining,
                    historical_rate,
                    historical_before_published_ts,
                ),
            )
            historical_batches_since_retry = 0
            continue
        forward_started = time.monotonic()
        rows = list_historical_articles_newest(
            before_published_ts=historical_before_published_ts,
            before_url=historical_before_url,
            limit=BATCH_SIZE,
            min_published_ts=MIN_PUBLISHED_TS,
        )
        selection_seconds = time.monotonic() - forward_started
        if not rows:
            retry_pending = (
                persistent_store.count_historical_article_retries()
                if PROCESS_RETRIES else 0
            )
            if retry_pending:
                historical_batches_since_retry = RETRY_EVERY_BATCHES
                _heartbeat(
                    "waiting_for_historical_retries",
                    phase=phase,
                    calendar_completed=calendar_completed,
                    historical_completed=historical_completed,
                    retry_pending=retry_pending,
                )
                time.sleep(MODEL_WAIT_SECONDS)
                continue
            phase = "complete"
            break
        lookup_started = time.monotonic()
        existing_urls = persistent_store.existing_historical_article_enrichment_urls(
            [row.get("url") for row in rows]
        )
        pending_rows = [row for row in rows if row.get("url") not in existing_urls]
        lookup_seconds = time.monotonic() - lookup_started
        _heartbeat(
            "processing_historical_articles",
            phase=phase,
            calendar_completed=calendar_completed,
            historical_completed=historical_completed,
            batch_size=len(pending_rows),
            skipped_existing=len(rows) - len(pending_rows),
            current_url=rows[0].get("url"),
            **_historical_progress(
                historical_total,
                historical_remaining,
                historical_rate,
                int(rows[0].get("published_ts") or historical_before_published_ts),
            ),
        )
        stats = process_batch(pending_rows) if pending_rows else {
            "processed": 0,
            "failed": 0,
            "completed_delta": 0,
        }
        historical_before_published_ts = int(rows[-1].get("published_ts") or 0)
        historical_before_url = str(rows[-1]["url"])
        historical_remaining = max(0, historical_remaining - len(rows))
        historical_completed += stats.get("completed_delta", stats["processed"])
        historical_state = {
            "before_published_ts": historical_before_published_ts,
            "before_url": historical_before_url,
            "completed": historical_completed,
            "pipeline_version": PIPELINE_VERSION,
            "newest_first_v2": True,
        }
        checkpoint_started = time.monotonic()
        persistent_store.save_worker_checkpoints({
            HISTORICAL_CHECKPOINT_NAME: historical_state,
            CHECKPOINT_NAME: {
                "phase": phase,
                "cutoff_ts": cutoff_ts,
                "calendar_after_id": calendar_after_id,
                "calendar_completed": calendar_completed,
                "historical_before_published_ts": historical_before_published_ts,
                "historical_before_url": historical_before_url,
                "historical_start_published_ts": historical_start_published_ts,
                "historical_completed": historical_completed,
                "retry_accounting_v2": retry_accounting_v2,
                "newest_first_v2": newest_first_v2,
            },
        })
        checkpoint_seconds = time.monotonic() - checkpoint_started
        historical_rate = forward_rate.record(len(rows), forward_started, time.monotonic())
        _heartbeat(
            "historical_articles_ok",
            phase=phase,
            calendar_completed=calendar_completed,
            historical_completed=historical_completed,
            batch_size=len(rows),
            failed=stats["failed"],
            skipped_existing=len(rows) - len(pending_rows),
            selection_seconds=round(selection_seconds, 3),
            existing_lookup_seconds=round(lookup_seconds, 3),
            checkpoint_seconds=round(checkpoint_seconds, 3),
            forward_seconds=round(time.monotonic() - forward_started, 3),
            **{key: stats[key] for key in (
                "duration_seconds", "resolve_seconds", "inference_seconds",
                "persist_seconds", "records_per_minute",
            ) if key in stats},
            **_historical_progress(
                historical_total,
                historical_remaining,
                historical_rate,
                historical_before_published_ts,
            ),
        )
        historical_batches_since_retry += 1

    persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, {
        "phase": "complete",
        "cutoff_ts": cutoff_ts,
        "calendar_after_id": calendar_after_id,
        "calendar_completed": calendar_completed,
        "historical_before_published_ts": historical_before_published_ts,
        "historical_before_url": historical_before_url,
        "historical_start_published_ts": historical_start_published_ts,
        "historical_completed": historical_completed,
        "retry_accounting_v2": retry_accounting_v2,
        "newest_first_v2": newest_first_v2,
    })
    _heartbeat(
        "complete",
        phase="complete",
        calendar_completed=calendar_completed,
        historical_completed=historical_completed,
        **_historical_progress(
            historical_total,
            historical_remaining,
            historical_rate,
            historical_before_published_ts,
        ),
    )


def run_resilient() -> None:
    while True:
        try:
            run()
            return
        except Exception as exc:
            logger.exception("News backlog worker interrupted; resuming from its checkpoint")
            try:
                _heartbeat("waiting_after_error", error=str(exc)[:500])
            except Exception:
                pass
            time.sleep(MODEL_WAIT_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting one-shot news backlog and historical article worker")
    run_resilient()
