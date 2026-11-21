import json
import logging
import os
import time
from threading import Event
from typing import Any, Callable

import redis


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PENDING_KEY = "news:index:pending"
PROCESSING_KEY = "news:index:processing"
DEAD_LETTER_KEY = "news:index:dead_letter"
MAX_BATCH_SIZE = max(1, int(os.getenv("NEWS_INDEX_QUEUE_BATCH_SIZE", "40")))
MAX_RETRIES = max(1, int(os.getenv("NEWS_INDEX_QUEUE_MAX_RETRIES", "5")))
RETRY_DELAY_SECONDS = max(1, int(os.getenv("NEWS_INDEX_QUEUE_RETRY_DELAY_SECONDS", "30")))


def _client():
    return redis.from_url(REDIS_URL, decode_responses=True)


def _encode(item: dict[str, Any], attempts: int = 0, error: str = "") -> str:
    return json.dumps({
        "item": item,
        "attempts": attempts,
        "error": error[:500],
    }, separators=(",", ":"))


def _decode(payload: str) -> tuple[dict[str, Any], int]:
    value = json.loads(payload)
    if not isinstance(value.get("item"), dict):
        raise ValueError("News index queue payload has no item")
    return value["item"], max(0, int(value.get("attempts") or 0))


def enqueue(items: list[dict[str, Any]], client=None) -> int:
    if not items:
        return 0
    client = client or _client()
    client.lpush(PENDING_KEY, *[_encode(item) for item in items])
    return len(items)


def _recover(client) -> None:
    while client.rpoplpush(PROCESSING_KEY, PENDING_KEY) is not None:
        pass


def consume_forever(
    index_batch: Callable[[list[dict[str, Any]]], None],
    stop_event: Event,
    client=None,
) -> None:
    client = client or _client()
    _recover(client)
    while not stop_event.is_set():
        claimed = []
        for _ in range(MAX_BATCH_SIZE):
            payload = client.rpoplpush(PENDING_KEY, PROCESSING_KEY)
            if payload is None:
                break
            claimed.append(payload)
        if not claimed:
            stop_event.wait(1)
            continue
        decoded = []
        for payload in claimed:
            try:
                item, attempts = _decode(payload)
            except Exception as exc:
                pipe = client.pipeline()
                pipe.lrem(PROCESSING_KEY, 1, payload)
                pipe.lpush(DEAD_LETTER_KEY, json.dumps({
                    "raw_payload": payload[:2000],
                    "error": str(exc)[:500],
                }, separators=(",", ":")))
                pipe.execute()
            else:
                decoded.append((payload, item, attempts))
        if not decoded:
            continue
        try:
            index_batch([item for _payload, item, _attempts in decoded])
        except Exception:
            logger.exception("Supplemental news index batch failed; isolating records")
            pipe = client.pipeline()
            failed = 0
            for payload, item, attempts in decoded:
                try:
                    index_batch([item])
                except Exception as exc:
                    failed += 1
                    attempts += 1
                    target = DEAD_LETTER_KEY if attempts >= MAX_RETRIES else PENDING_KEY
                    pipe.lrem(PROCESSING_KEY, 1, payload)
                    pipe.lpush(target, _encode(item, attempts, str(exc)))
                else:
                    pipe.lrem(PROCESSING_KEY, 1, payload)
            pipe.execute()
            if failed:
                stop_event.wait(RETRY_DELAY_SECONDS)
        else:
            pipe = client.pipeline()
            for payload, _item, _attempts in decoded:
                pipe.lrem(PROCESSING_KEY, 1, payload)
            pipe.execute()


def queue_status(client=None) -> dict[str, int]:
    client = client or _client()
    return {
        "pending": int(client.llen(PENDING_KEY) or 0),
        "processing": int(client.llen(PROCESSING_KEY) or 0),
        "dead_letter": int(client.llen(DEAD_LETTER_KEY) or 0),
    }
