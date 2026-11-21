import faulthandler
import hashlib
import json
import logging
import os
import time

import redis
import requests

from services import persistent_store
from services.calendar_relevance import (
    PartialClassificationError,
    configured,
    is_automatically_accepted,
    prepare_classification_payload,
    should_remove,
)
from services.news_article_resolver import resolve_article_batch
from services.financial_news_gate import classify_articles
from services.news_rejection import receipt as rejection_receipt, known_rejections
from services.active_news_store import remove_unrelated_article
from services.calendar_relevance_queue import (
    ACCEPTED_TICKERS_KEY,
    BACKLOG_PENDING_KEY,
    CLASSIFIED_KEY,
    ENRICHMENTS_KEY,
    FAILED_KEY,
    HEARTBEAT_KEY,
    LIVE_PENDING_KEY,
    QUEUED_KEY,
    REDIS_URL,
    WATCH_KEY,
    enqueue,
    migrate_legacy_pending,
    pending_counts,
    processing_key,
    promote_recent_backlog,
    recover_processing,
)


logger = logging.getLogger(__name__)
INFLIGHT_RESULTS_KEY = "calendar:relevance:inflight_results"
faulthandler.enable()
BATCH_SIZE = max(1, min(int(os.getenv("CALENDAR_LLM_BATCH_SIZE", "25")), 50))
POLL_SECONDS = max(2, int(os.getenv("CALENDAR_LLM_POLL_SECONDS", "10")))
MAX_ITEM_ATTEMPTS = max(1, int(os.getenv("CALENDAR_LLM_MAX_ITEM_ATTEMPTS", "3")))
BACKLOG_REFILL_SIZE = max(25, min(int(os.getenv("CALENDAR_LLM_BACKLOG_REFILL_SIZE", "500")), 5000))
SUPERVISOR_RETRY_SECONDS = max(5, int(os.getenv("CALENDAR_LLM_SUPERVISOR_RETRY_SECONDS", "15")))
RECENT_ARTICLES_KEY = "news:recent:articles"
r = redis.from_url(REDIS_URL, decode_responses=True)


def _heartbeat(status: str, **extra):
    counts = pending_counts(r)
    r.set(HEARTBEAT_KEY, json.dumps({
        "status": status,
        "ts": int(time.time()),
        "pending": counts["total"],
        "live_pending": counts["live"],
        "backlog_pending": counts["backlog"],
        "processing": counts["processing"],
        "failed": int(r.llen(FAILED_KEY) or 0),
        **extra,
    }))


def _watch(phase: str, items: list[dict], queue_name: str, started_at: int, **extra):
    r.set(WATCH_KEY, json.dumps({
        "phase": phase,
        "queue": queue_name,
        "started_at": started_at,
        "updated_at": int(time.time()),
        "batch_size": len(items),
        "inputs": prepare_classification_payload(items),
        **extra,
    }))


def _rebuild_accepted_ticker_index() -> int:
    ticker_scores: dict[str, float] = {}
    article_rows = r.zrange(RECENT_ARTICLES_KEY, 0, -1, withscores=True)
    for start in range(0, len(article_rows), 500):
        batch = article_rows[start:start + 500]
        ids = [article_id for article_id, _score in batch]
        enrichments = r.hmget(ENRICHMENTS_KEY, ids)
        for (article_id, score), raw in zip(batch, enrichments):
            try:
                result = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                continue
            if (
                not is_automatically_accepted(result)
                or result.get("relevance_label") not in {"finance", "business", "markets"}
            ):
                continue
            for ticker in result.get("related_tickers") or []:
                symbol = str(ticker or "").strip().upper()
                if symbol:
                    ticker_scores[symbol] = max(float(score), ticker_scores.get(symbol, 0))
    pipe = r.pipeline()
    pipe.delete(ACCEPTED_TICKERS_KEY)
    if ticker_scores:
        pipe.zadd(ACCEPTED_TICKERS_KEY, ticker_scores)
    pipe.execute()
    return len(ticker_scores)


def _take_batch() -> tuple[list[dict], str]:
    queue_name = "live" if r.llen(LIVE_PENDING_KEY) else "backlog"
    pending_key = LIVE_PENDING_KEY if queue_name == "live" else BACKLOG_PENDING_KEY
    active_key = processing_key(queue_name)
    items = []
    for _ in range(BATCH_SIZE):
        raw = r.lmove(pending_key, active_key, "LEFT", "RIGHT")
        if not raw:
            break
        try:
            item = json.loads(raw)
            item["_queue_raw"] = raw
            items.append(item)
        except (TypeError, ValueError):
            r.lrem(active_key, 1, raw)
            continue
    return items, queue_name


def _ack(item: dict, queue_name: str) -> None:
    raw = item.get("_queue_raw")
    if raw:
        r.lrem(processing_key(queue_name), 1, raw)


def _queue_payload(item: dict) -> str:
    return json.dumps({key: value for key, value in item.items() if key != "_queue_raw"})


def _move_processing_item(item: dict, queue_name: str, destination: str, payload: str,
                          *, terminal: bool = False) -> bool:
    active = processing_key(queue_name)
    raw = item.get("_queue_raw")
    for _ in range(10):
        with r.pipeline() as pipe:
            try:
                pipe.watch(active, destination, QUEUED_KEY)
                for key, expected in ((active, "list"), (destination, "list"), (QUEUED_KEY, "set")):
                    kind = pipe.type(key)
                    if isinstance(kind, bytes):
                        kind = kind.decode()
                    if kind not in {"none", expected}:
                        raise ValueError(f"Unexpected Redis type for {key}: {kind}")
                if raw and pipe.lpos(active, raw) is None:
                    return False
                pipe.multi()
                pipe.rpush(destination, payload)
                if raw:
                    pipe.lrem(active, 1, raw)
                if terminal:
                    pipe.srem(QUEUED_KEY, item["id"])
                pipe.execute()
                return True
            except redis.WatchError:
                continue
    raise RuntimeError("Processing-item move deferred because the queue kept changing")


def _requeue(items: list[dict], queue_name: str):
    if items:
        pending_key = LIVE_PENDING_KEY if queue_name == "live" else BACKLOG_PENDING_KEY
        # A failing item must not immediately block newer work again.
        for item in items:
            if r.sismember(CLASSIFIED_KEY, item["id"]):
                _ack(item, queue_name)
                continue
            _move_processing_item(item, queue_name, pending_key, _queue_payload(item))


def _input_fingerprint(item: dict) -> str:
    payload = {key: value for key, value in item.items()
               if key != "_queue_raw" and not key.startswith("_llm_")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _prepare_inference(items: list[dict], *, on_inference=None) -> tuple[list[dict], dict, list]:
    """Stage inference before projection; reuse only matching queued inputs."""
    cached = r.hmget(INFLIGHT_RESULTS_KEY, [item["id"] for item in items])
    results, pending, restored = {}, [], {}
    originals = {item["id"]: _input_fingerprint(item) for item in items}
    rejected = known_rejections(items)
    for item, raw in zip(items, cached):
        if item.get("url") in rejected:
            restored[item["id"]] = item
            results[item["id"]] = rejected[item["url"]]
            continue
        try:
            entry = json.loads(raw) if raw else {}
            valid = (isinstance(entry, dict) and originals[item["id"]] in entry.get("inputs", [])
                     and isinstance(entry.get("item"), dict) and entry["item"].get("id") == item["id"]
                     and isinstance(entry.get("result"), dict) and bool(entry["result"])
                     and not entry["result"].get("processing_error")
                     and entry["result"].get("relevance_source") != "processing_failure")
        except (TypeError, ValueError):
            valid = False
        if valid:
            restored[item["id"]] = {**entry["item"], "_queue_raw": item.get("_queue_raw")}
            results[item["id"]] = entry["result"]
        else:
            pending.append(item)
    failures = []
    if pending:
        resolved = resolve_article_batch(pending)
        if on_inference:
            on_inference(resolved)
        inferred, failures = _classify_resilient(resolved)
        staged = {}
        for item in resolved:
            key = item["id"]
            restored[key] = item
            result = inferred.get(key)
            if result and not result.get("processing_error") and result.get("relevance_source") != "processing_failure":
                staged[key] = json.dumps({
                    "inputs": [originals[key], _input_fingerprint(item)],
                    "item": {k: v for k, v in item.items() if k != "_queue_raw"}, "result": result,
                })
        if staged:
            r.hset(INFLIGHT_RESULTS_KEY, mapping=staged)
        results.update(inferred)
    return [restored[item["id"]] for item in items], results, failures

def _refill_unclassified_backlog() -> int:
    if not persistent_store.enabled():
        return 0
    failed_ids = set()
    for raw in r.lrange(FAILED_KEY, 0, -1):
        try:
            failed_ids.add(str(json.loads(raw).get("id") or ""))
        except (TypeError, ValueError):
            continue
    unclassified = persistent_store.list_unclassified_market_news(BACKLOG_REFILL_SIZE)
    remaining = max(0, BACKLOG_REFILL_SIZE - len(unclassified))
    verification = (
        persistent_store.list_market_news_needing_verification(remaining)
        if remaining
        else []
    )
    events = [
        event
        for event in [*unclassified, *verification]
        if event.get("id") and event["id"] not in failed_ids
    ]
    for event in events:
        r.srem(CLASSIFIED_KEY, event["id"])
        r.srem(QUEUED_KEY, event["id"])
    return enqueue(events, client=r, priority="backlog")


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
        left_results, left_failed = _classify_resilient(items[:midpoint])
        right_results, right_failed = _classify_resilient(items[midpoint:])
        return {**left_results, **right_results}, left_failed + right_failed
    missing = [item for item in items if item["id"] not in results]
    if not missing:
        return results, []
    if len(missing) == 1:
        if len(items) > 1:
            recovered, failed = _classify_resilient(missing)
            return {**results, **recovered}, failed
        return results, [(items[0], "Model response omitted the requested article ID")]
    midpoint = len(missing) // 2
    left_results, left_failed = _classify_resilient(missing[:midpoint])
    right_results, right_failed = _classify_resilient(missing[midpoint:])
    return {
        **results,
        **left_results,
        **right_results,
    }, left_failed + right_failed


def _provider_unavailable(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        return response is None or int(response.status_code) >= 500
    return False


def _handle_failed(items: list[tuple[dict, str]], queue_name: str) -> int:
    retry = []
    failed = 0
    for item, error in items:
        attempts = int(item.get("_llm_attempts") or 0) + 1
        item["_llm_attempts"] = attempts
        item["_llm_error"] = error[:500]
        if attempts < MAX_ITEM_ATTEMPTS:
            retry.append(item)
            continue
        failed_item = {key: value for key, value in item.items() if key != "_queue_raw"}
        failed += int(_move_processing_item(
            item, queue_name, FAILED_KEY, json.dumps({**failed_item, "_queue": queue_name}), terminal=True,
        ))
    _requeue(retry, queue_name)
    return failed


def run_forever():
    _heartbeat("starting")
    recovered = recover_processing(r)
    if recovered:
        logger.warning("Recovered %s interrupted Calendar relevance items", recovered)
    _heartbeat("normalizing_calendar")
    normalized = persistent_store.normalize_calendar_news_ai_projection()
    if normalized:
        logger.info("Normalized %s Calendar market-news AI projections", normalized)
    moved = migrate_legacy_pending(r)
    if moved:
        logger.info("Moved %s existing relevance items into the historical backlog", moved)
    promoted = promote_recent_backlog(r)
    if promoted:
        logger.info("Promoted %s recent relevance items ahead of the historical backlog", promoted)
    _heartbeat("rebuilding_ticker_index")
    accepted_tickers = _rebuild_accepted_ticker_index()
    logger.info("Rebuilt accepted News LLM ticker index with %s symbols", accepted_tickers)
    while True:
        if not configured():
            _heartbeat("waiting_for_configuration")
            time.sleep(60)
            continue
        items, queue_name = _take_batch()
        if not items:
            refilled = _refill_unclassified_backlog()
            if refilled:
                _heartbeat("backlog_refilled", refilled=refilled)
                logger.info("Queued %s durable articles requiring classification or verification", refilled)
                continue
            _heartbeat("idle")
            time.sleep(POLL_SECONDS)
            continue
        started_at = int(time.time())
        _watch("resolving", items, queue_name, started_at)
        try:
            items, results, classification_failures = _prepare_inference(
                items, on_inference=lambda resolved: _watch("inference", resolved, queue_name, started_at),
            )
            removed = classified = uncertain = 0
            missing = []
            failed_ids = {item["id"] for item, _error in classification_failures}
            for item in items:
                event_id = item["id"]
                if event_id in failed_ids:
                    continue
                result = results.get(event_id)
                if not result:
                    missing.append(item)
                    continue
                result = rejection_receipt(result)
                r.hset(ENRICHMENTS_KEY, event_id, json.dumps(result))
                if (
                    is_automatically_accepted(result)
                    and result.get("relevance_label") in {"finance", "business", "markets"}
                ):
                    published_ts = max(1, int(item.get("published_ts") or started_at))
                    for ticker in result.get("related_tickers") or []:
                        r.zadd(ACCEPTED_TICKERS_KEY, {ticker: published_ts}, gt=True)
                if should_remove(result):
                    if item.get("url"):
                        persistent_store.save_historical_article_results([(item, result)], "finance-gate-v1")
                    removed += int(persistent_store.delete_calendar_event(event_id))
                    remove_unrelated_article(r, item)
                else:
                    classified += int(persistent_store.update_calendar_event_relevance(event_id, result))
                    uncertain += int(result.get("relevance_label") == "uncertain")
                with r.pipeline() as pipe:
                    pipe.sadd(CLASSIFIED_KEY, event_id)
                    pipe.srem(QUEUED_KEY, event_id)
                    if item.get("_queue_raw"):
                        pipe.lrem(processing_key(queue_name), 1, item["_queue_raw"])
                    pipe.hdel(INFLIGHT_RESULTS_KEY, event_id)
                    pipe.execute()
            failed = _handle_failed(classification_failures, queue_name)
            _requeue(missing, queue_name)
            _watch(
                "completed",
                items,
                queue_name,
                started_at,
                completed_at=int(time.time()),
                outputs=results,
                failures=[
                    {"id": item["id"], "error": error[:500]}
                    for item, error in classification_failures
                ],
            )
            duration_seconds = max(1, int(time.time()) - started_at)
            _heartbeat(
                "ok",
                active_queue=queue_name,
                batch=len(items),
                duration_seconds=duration_seconds,
                records_per_minute=round(len(items) * 60 / duration_seconds, 2),
                classified=classified,
                removed=removed,
                uncertain=uncertain,
                retried=len(missing),
                isolated_failures=len(classification_failures),
                dead_lettered=failed,
            )
        except Exception as exc:
            logger.warning("Calendar relevance batch failed: %s", exc)
            _requeue(items, queue_name)
            _watch("error", items, queue_name, started_at, error=str(exc)[:500])
            _heartbeat("error", active_queue=queue_name, error=str(exc))
            time.sleep(60)
        else:
            time.sleep(POLL_SECONDS)


def run_resilient() -> None:
    while True:
        try:
            run_forever()
            return
        except Exception:
            logger.exception(
                "Calendar relevance worker loop failed; recovering queued work"
            )
            time.sleep(SUPERVISOR_RETRY_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting Calendar relevance worker")
    run_resilient()
