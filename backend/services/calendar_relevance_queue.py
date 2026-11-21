import json
import os
import time

import redis


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
LEGACY_PENDING_KEY = "calendar:relevance:pending"
LIVE_PENDING_KEY = "calendar:relevance:pending:live"
BACKLOG_PENDING_KEY = "calendar:relevance:pending:backlog"
LIVE_PROCESSING_KEY = "calendar:relevance:processing:live"
BACKLOG_PROCESSING_KEY = "calendar:relevance:processing:backlog"
# Retained for callers that only need the high-priority queue key.
PENDING_KEY = LIVE_PENDING_KEY
QUEUED_KEY = "calendar:relevance:queued"
CLASSIFIED_KEY = "calendar:relevance:classified"
HEARTBEAT_KEY = "calendar:relevance:heartbeat"
WATCH_KEY = "calendar:relevance:watch"
ENRICHMENTS_KEY = "news:llm:enrichments"
ACCEPTED_TICKERS_KEY = "news:llm:accepted:tickers"
FAILED_KEY = "calendar:relevance:failed"
RECENT_ARTICLES_KEY = "news:recent:articles"
RECENT_PRIORITY_SECONDS = max(900, int(os.getenv("CALENDAR_LLM_RECENT_PRIORITY_SECONDS", "86400")))


def _queue_key(priority: str) -> str:
    if priority not in {"live", "backlog"}:
        raise ValueError("priority must be live or backlog")
    return LIVE_PENDING_KEY if priority == "live" else BACKLOG_PENDING_KEY


def processing_key(priority: str) -> str:
    if priority not in {"live", "backlog"}:
        raise ValueError("priority must be live or backlog")
    return LIVE_PROCESSING_KEY if priority == "live" else BACKLOG_PROCESSING_KEY


def enqueue(events: list[dict], client=None, *, priority: str = "live") -> int:
    client = client or redis.from_url(REDIS_URL, decode_responses=True)
    pending_key = _queue_key(priority)
    added = 0
    payloads = []
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id or client.sismember(CLASSIFIED_KEY, event_id) or client.sadd(QUEUED_KEY, event_id) != 1:
            continue
        details = event.get("details") or {}
        payloads.append(json.dumps({
            "id": event_id,
            "title": event.get("title") or "",
            "summary": details.get("summary") or "",
            "source": event.get("source") or "",
            "symbols": event.get("symbols") or [],
            "url": event.get("url") or "",
            "source_urls": details.get("source_urls") or [],
            "event_ids": details.get("event_ids") or [],
            "title_inferred": bool(details.get("title_inferred")),
            "published_ts": int(details.get("published_ts") or 0),
        }))
        added += 1
    if payloads:
        if priority == "live":
            # LPOP consumes the head. Put the newest incoming batch ahead of
            # older live work while preserving order within this batch.
            client.lpush(pending_key, *reversed(payloads))
        else:
            for payload in payloads:
                client.rpush(pending_key, payload)
    return added


def migrate_legacy_pending(client=None) -> int:
    client = client or redis.from_url(REDIS_URL, decode_responses=True)
    moved = 0
    while client.rpoplpush(LEGACY_PENDING_KEY, BACKLOG_PENDING_KEY) is not None:
        moved += 1
    return moved


def pending_counts(client=None) -> dict[str, int]:
    client = client or redis.from_url(REDIS_URL, decode_responses=True)
    live = int(client.llen(LIVE_PENDING_KEY) or 0)
    backlog = int(client.llen(BACKLOG_PENDING_KEY) or 0)
    legacy = int(client.llen(LEGACY_PENDING_KEY) or 0)
    live_processing = int(client.llen(LIVE_PROCESSING_KEY) or 0)
    backlog_processing = int(client.llen(BACKLOG_PROCESSING_KEY) or 0)
    return {
        "live": live,
        "backlog": backlog + legacy,
        "processing": live_processing + backlog_processing,
        "total": live + backlog + legacy + live_processing + backlog_processing,
    }


def recover_processing(client=None) -> int:
    client = client or redis.from_url(REDIS_URL, decode_responses=True)
    recovered = 0
    for pending_key, active_key in (
        (LIVE_PENDING_KEY, LIVE_PROCESSING_KEY),
        (BACKLOG_PENDING_KEY, BACKLOG_PROCESSING_KEY),
    ):
        while True:
            raw = client.rpop(active_key)
            if raw is None:
                break
            client.lpush(pending_key, raw)
            recovered += 1
    return recovered


def replay_failed(client=None, *, limit: int = 0, error_filter: str | None = None) -> int:
    client = client or redis.from_url(REDIS_URL, decode_responses=True)
    available = int(client.llen(FAILED_KEY) or 0)
    target = available if limit <= 0 else min(available, limit)
    replayed = 0
    for _ in range(target):
        outcome = _replay_failed_head(client, error_filter=error_filter)
        if outcome is None:
            break
        replayed += int(outcome)
    return replayed


def _replay_failed_head(client, *, error_filter: str | None = None) -> bool | None:
    keys = {FAILED_KEY: "list", QUEUED_KEY: "set", CLASSIFIED_KEY: "set",
            LIVE_PENDING_KEY: "list", BACKLOG_PENDING_KEY: "list"}
    for _ in range(10):
        with client.pipeline() as pipe:
            try:
                pipe.watch(*keys)
                # MULTI cannot roll back runtime type errors: check types while watched.
                for key, expected in keys.items():
                    kind = pipe.type(key)
                    if isinstance(kind, bytes):
                        kind = kind.decode()
                    if kind not in {"none", expected}:
                        raise ValueError(f"Unexpected Redis type for {key}: {kind}")
                raw = pipe.lindex(FAILED_KEY, 0)
                if raw is None:
                    return None
                try:
                    item = json.loads(raw)
                except (TypeError, ValueError):
                    item = None
                event_id = str(item.get("id") or "") if isinstance(item, dict) else ""
                queue = str(item.get("_queue", "backlog")) if isinstance(item, dict) else ""
                valid = bool(event_id) and queue in {"live", "backlog"}
                selected = valid and (error_filter is None or item.get("_llm_error") == error_filter)
                replay = selected and not (pipe.sismember(CLASSIFIED_KEY, event_id)
                                       or pipe.sismember(QUEUED_KEY, event_id))
                payload = None
                if replay:
                    for field in ("_queue", "_llm_attempts", "_llm_error"):
                        item.pop(field, None)
                    payload = json.dumps(item)
                pipe.multi()
                pipe.lpop(FAILED_KEY)
                if not valid or not selected:
                    pipe.rpush(FAILED_KEY, raw)
                elif replay:
                    pipe.sadd(QUEUED_KEY, event_id)
                    pipe.rpush(_queue_key(queue), payload)
                pipe.execute()
                return bool(replay)
            except redis.WatchError:
                continue
    raise RuntimeError("Failed-article replay deferred because the queue kept changing")


def promote_recent_backlog(client=None, *, now: int | None = None) -> int:
    client = client or redis.from_url(REDIS_URL, decode_responses=True)
    cutoff = int(now or time.time()) - RECENT_PRIORITY_SECONDS
    recent_items = []
    live_size = int(client.llen(LIVE_PENDING_KEY) or 0)
    for order in range(live_size):
        raw = client.lpop(LIVE_PENDING_KEY)
        if raw is not None:
            recent_items.append(_priority_item(client, raw, order))
    backlog_size = int(client.llen(BACKLOG_PENDING_KEY) or 0)
    promoted = 0
    for order in range(live_size, live_size + backlog_size):
        raw = client.lpop(BACKLOG_PENDING_KEY)
        if raw is None:
            break
        item, published_ts, _order, _raw = _priority_item(client, raw, order)
        if item and published_ts >= cutoff:
            recent_items.append((item, published_ts, order, raw))
            promoted += 1
        else:
            client.rpush(BACKLOG_PENDING_KEY, raw)
    for _item, _published_ts, _order, raw in sorted(
        recent_items,
        key=lambda row: (-row[1], row[2]),
    ):
        client.rpush(LIVE_PENDING_KEY, raw)
    return promoted


def _priority_item(client, raw: str, order: int) -> tuple[dict, int, int, str]:
    try:
        item = json.loads(raw)
        score = client.zscore(RECENT_ARTICLES_KEY, str(item.get("id") or ""))
        published_ts = int(item.get("published_ts") or score or 0)
    except (TypeError, ValueError):
        item, published_ts = {}, 0
    return item, published_ts, order, raw
