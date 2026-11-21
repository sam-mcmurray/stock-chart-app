"""Both news GPUs consume the same durable newest-first historical queue."""
import time

from services import historical_work_queue as queue


def run(worker):
    queue.ensure_schema()
    worker.ensure_historical_article_index()
    queue.initialize(f"news_backfill:{worker.PIPELINE_VERSION}",
                     f"news_backfill:{worker.PIPELINE_VERSION}:older-pre-2018-11-09",
                     worker.newest_historical_article_timestamp,
                     worker.historical_article_partition_size)
    if worker.PROCESS_RETRIES:
        worker.persistent_store.seed_historical_article_retries(worker.PIPELINE_VERSION)
    rate = worker._ForwardProgressRate()
    batches = 0

    def heartbeat(status, **extra):
        worker._heartbeat(status, phase="historical_articles",
                          **{**queue.snapshot(worker.CHECKPOINT_NAME),
                             "historical_rate_per_minute": round(rate.rate, 2), **extra})

    while True:
        if not worker.configured():
            heartbeat("waiting_for_model")
            time.sleep(worker.MODEL_WAIT_SECONDS)
            continue
        live = worker.pending_counts(worker.r)
        if worker.YIELD_TO_LIVE_QUEUE and live["total"]:
            heartbeat("waiting_for_live_queue", live_queue=live)
            time.sleep(worker.LIVE_QUEUE_YIELD_SECONDS)
            continue
        if worker.PROCESS_RETRIES and batches >= worker.RETRY_EVERY_BATCHES:
            rows = worker.persistent_store.list_historical_article_retries(limit=worker.RETRY_BATCH_SIZE)
            if rows:
                heartbeat("retrying_historical_articles", batch_size=len(rows), current_url=rows[0]["url"])
                stats = worker.process_batch(rows)
                queue.record_retry(worker.CHECKPOINT_NAME, stats)
                heartbeat("historical_retry_ok", failed=stats["failed"])
            batches = 0
        started = time.monotonic()
        scanned = queue.fill(worker.list_historical_articles_newest, worker.BATCH_SIZE)
        with queue.claim() as claimed:
            if claimed is None:
                state = queue.snapshot(worker.CHECKPOINT_NAME)
                retries = worker.persistent_store.count_historical_article_retries()
                if state["source_exhausted"] and not state["shared_queue_pending"] and not retries:
                    heartbeat("complete")
                    return
                heartbeat("waiting_for_shared_work", retry_pending=retries)
                batches = worker.RETRY_EVERY_BATCHES
                if scanned and not state["shared_queue_pending"]:
                    continue
                time.sleep(worker.LIVE_QUEUE_YIELD_SECONDS)
                continue
            conn, job = claimed
            rows = job["articles"]
            selection = time.monotonic() - started
            heartbeat("processing_historical_articles", batch_size=len(rows), current_url=rows[0]["url"],
                      historical_cursor_published_ts=int(rows[0].get("published_ts") or 0))
            stats = worker.process_batch(rows)
            queue.finish(conn, job, worker.CHECKPOINT_NAME, stats)
            rate.record(len(rows), started, time.monotonic())
            heartbeat("historical_articles_ok", batch_size=len(rows), failed=stats["failed"],
                      skipped_existing=stats.get("reused_results", 0),
                      selection_seconds=round(selection, 3),
                      forward_seconds=round(time.monotonic() - started, 3),
                      **{k: stats[k] for k in ("duration_seconds", "resolve_seconds",
                                             "inference_seconds", "persist_seconds") if k in stats})
            batches += 1
