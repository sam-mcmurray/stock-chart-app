import json
import logging
import os
import time
from threading import Event
from typing import Any, Callable

import redis


logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PENDING_KEY = "gdelt:events:index:pending"
PROCESSING_KEY = "gdelt:events:index:processing"
DEAD_LETTER_KEY = "gdelt:events:index:dead_letter"
MAX_BATCH_SIZE = max(1, int(os.getenv("GDELT_EVENT_QUEUE_BATCH_SIZE", "40")))
MAX_RETRIES = max(1, int(os.getenv("GDELT_EVENT_QUEUE_MAX_RETRIES", "5")))
MIN_DEAD_LETTER_AGE_SECONDS = max(0, int(os.getenv("GDELT_EVENT_QUEUE_MIN_DEAD_LETTER_AGE_SECONDS", "3600")))
RETRY_DELAY_SECONDS = max(1, int(os.getenv("GDELT_EVENT_QUEUE_RETRY_DELAY_SECONDS", "30")))
QUEUE_VERSION = 1


def _redis_client():
    return redis.from_url(REDIS_URL, decode_responses=True)


def _encode_payload(
    item: dict[str, Any],
    *,
    attempts: int = 0,
    first_failed_ts: int = 0,
    last_error: str = "",
) -> str:
    envelope = {
        "queue_version": QUEUE_VERSION,
        "attempts": attempts,
        "first_failed_ts": first_failed_ts,
        "item": item,
    }
    if last_error:
        envelope["last_error"] = last_error[:500]
        envelope["last_failed_ts"] = int(time.time())
    return json.dumps(envelope, separators=(",", ":"))


def _decode_payload(payload: str) -> tuple[dict[str, Any], int, int]:
    decoded = json.loads(payload)
    if decoded.get("queue_version") == QUEUE_VERSION:
        if not isinstance(decoded.get("item"), dict):
            raise ValueError("Versioned GDELT queue payload has no replayable item")
        return (
            decoded["item"],
            max(0, int(decoded.get("attempts") or 0)),
            max(0, int(decoded.get("first_failed_ts") or 0)),
        )
    if not isinstance(decoded, dict):
        raise ValueError("GDELT queue payload must contain an object")
    return decoded, 0, 0


def enqueue(items: list[dict[str, Any]], client=None) -> int:
    if not items:
        return 0
    client = client or _redis_client()
    payloads = [_encode_payload(item) for item in items]
    client.lpush(PENDING_KEY, *payloads)
    return len(payloads)


def _claim_batch(client) -> list[str]:
    claimed = []
    for _ in range(MAX_BATCH_SIZE):
        payload = client.rpoplpush(PENDING_KEY, PROCESSING_KEY)
        if payload is None:
            break
        claimed.append(payload)
    return claimed


def _recover_inflight(client) -> int:
    recovered = 0
    while True:
        payload = client.rpoplpush(PROCESSING_KEY, PENDING_KEY)
        if payload is None:
            return recovered
        recovered += 1


def replay_dead_letters(limit: int = 100, client=None) -> int:
    client = client or _redis_client()
    replayed = 0
    for _ in range(max(0, limit)):
        candidate = client.lindex(DEAD_LETTER_KEY, -1)
        if candidate is None:
            break
        try:
            _decode_payload(candidate)
        except Exception:
            logger.error("Dead-letter replay stopped at a malformed, non-replayable payload")
            break
        payload = client.rpoplpush(DEAD_LETTER_KEY, PENDING_KEY)
        if payload is None:
            break
        replayed += 1
    return replayed


def queue_status(client=None) -> dict[str, int]:
    client = client or _redis_client()
    return {
        "pending": int(client.llen(PENDING_KEY) or 0),
        "processing": int(client.llen(PROCESSING_KEY) or 0),
        "dead_letter": int(client.llen(DEAD_LETTER_KEY) or 0),
    }


def consume_forever(
    index_batch: Callable[[list[dict[str, Any]]], None],
    stop_event: Event,
    client=None,
) -> None:
    client = client or _redis_client()
    recovered = _recover_inflight(client)
    if recovered:
        logger.info("Recovered %s unacknowledged GDELT events", recovered)

    while not stop_event.is_set():
        claimed = _claim_batch(client)
        if not claimed:
            stop_event.wait(1)
            continue
        decoded = []
        valid_claimed = []
        malformed_pipe = client.pipeline()
        malformed_count = 0
        for payload in claimed:
            try:
                decoded.append(_decode_payload(payload))
                valid_claimed.append(payload)
            except Exception as exc:
                malformed_count += 1
                malformed_pipe.lrem(PROCESSING_KEY, 1, payload)
                malformed_pipe.lpush(DEAD_LETTER_KEY, json.dumps({
                    "queue_version": QUEUE_VERSION,
                    "attempts": MAX_RETRIES,
                    "first_failed_ts": int(time.time()),
                    "last_failed_ts": int(time.time()),
                    "last_error": f"Malformed queue payload: {exc}"[:500],
                    "raw_payload": payload[:2000],
                }, separators=(",", ":")))
        if malformed_count:
            malformed_pipe.execute()
            logger.error("Moved %s malformed GDELT queue payloads to dead-letter", malformed_count)
        claimed = valid_claimed
        if not claimed:
            continue
        try:
            items = [item for item, _attempts, _first_failed_ts in decoded]
            index_batch(items)
            pipe = client.pipeline()
            for payload in claimed:
                pipe.lrem(PROCESSING_KEY, 1, payload)
            pipe.execute()
        except Exception as batch_exc:
            logger.exception("Canonical GDELT event batch indexing failed; isolating records")
            pipe = client.pipeline()
            failed = 0
            now_ts = int(time.time())
            for payload, (item, attempts, first_failed_ts) in zip(claimed, decoded):
                try:
                    index_batch([item])
                except Exception as item_exc:
                    failed += 1
                    attempts += 1
                    first_failed_ts = first_failed_ts or now_ts
                    error = f"{type(item_exc).__name__}: {item_exc}" or str(batch_exc)
                    retry_payload = _encode_payload(
                        item,
                        attempts=attempts,
                        first_failed_ts=first_failed_ts,
                        last_error=error,
                    )
                    old_enough = now_ts - first_failed_ts >= MIN_DEAD_LETTER_AGE_SECONDS
                    pipe.lrem(PROCESSING_KEY, 1, payload)
                    if attempts >= MAX_RETRIES and old_enough:
                        pipe.lpush(DEAD_LETTER_KEY, retry_payload)
                        logger.error("GDELT event moved to dead-letter queue after %s attempts", attempts)
                    else:
                        pipe.rpush(PENDING_KEY, retry_payload)
                else:
                    pipe.lrem(PROCESSING_KEY, 1, payload)
            pipe.execute()
            if failed:
                stop_event.wait(RETRY_DELAY_SECONDS)
