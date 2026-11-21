import json
import logging
import os
from threading import Event
from typing import Callable

import redis


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PENDING_KEY = "news:cleanup:pending"
PROCESSING_KEY = "news:cleanup:processing"
QUEUED_KEY = "news:cleanup:queued"
BATCH_SIZE = max(1, min(int(os.getenv("NEWS_CLEANUP_BATCH_SIZE", "100")), 500))


def _redis_client():
    return redis.from_url(REDIS_URL, decode_responses=True)


def enqueue(article_ids: list[str], client=None) -> int:
    client = client or _redis_client()
    added = 0
    for article_id in article_ids:
        normalized = str(article_id or "").strip()
        if not normalized or client.sadd(QUEUED_KEY, normalized) != 1:
            continue
        client.lpush(PENDING_KEY, json.dumps({"id": normalized}))
        added += 1
    return added


def _recover_inflight(client) -> int:
    recovered = 0
    while client.rpoplpush(PROCESSING_KEY, PENDING_KEY) is not None:
        recovered += 1
    return recovered


def consume_forever(
    delete_batch: Callable[[list[str]], None],
    stop_event: Event,
    client=None,
) -> None:
    client = client or _redis_client()
    recovered = _recover_inflight(client)
    if recovered:
        logger.info("Recovered %s unacknowledged news cleanup requests", recovered)
    while not stop_event.is_set():
        claimed = []
        article_ids = []
        for _ in range(BATCH_SIZE):
            payload = client.rpoplpush(PENDING_KEY, PROCESSING_KEY)
            if payload is None:
                break
            try:
                article_id = str(json.loads(payload).get("id") or "").strip()
            except (TypeError, ValueError):
                article_id = ""
            if not article_id:
                client.lrem(PROCESSING_KEY, 1, payload)
                continue
            claimed.append(payload)
            article_ids.append(article_id)
        if not claimed:
            stop_event.wait(1)
            continue
        try:
            delete_batch(article_ids)
        except Exception:
            logger.exception("News vector cleanup batch failed")
            pipe = client.pipeline()
            for payload in claimed:
                pipe.lrem(PROCESSING_KEY, 1, payload)
                pipe.rpush(PENDING_KEY, payload)
            pipe.execute()
            stop_event.wait(30)
            continue
        pipe = client.pipeline()
        for payload, article_id in zip(claimed, article_ids):
            pipe.lrem(PROCESSING_KEY, 1, payload)
            pipe.srem(QUEUED_KEY, article_id)
        pipe.execute()
