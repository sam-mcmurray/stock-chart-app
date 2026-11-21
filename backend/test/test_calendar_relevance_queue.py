import json
from unittest.mock import MagicMock

import requests

from services import calendar_relevance_queue
from workers import calendar_relevance_worker


class FakeRedis:
    def __init__(self):
        self.lists = {}
        self.sets = {}
        self.scores = {}

    def pipeline(self):
        client = self

        class Pipeline:
            def __init__(self):
                self.commands = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def watch(self, *keys):
                self.commands = None

            def multi(self):
                self.commands = []

            def execute(self):
                return [getattr(client, name)(*args) for name, args in self.commands]

            def __getattr__(self, name):
                def command(*args):
                    if self.commands is None:
                        return getattr(client, name)(*args)
                    self.commands.append((name, args))
                    return self
                return command

        return Pipeline()

    def type(self, key):
        return "list" if key in self.lists else "set" if key in self.sets else "none"

    def lindex(self, key, index):
        values = self.lists.get(key, [])
        return values[index] if len(values) > index else None

    def lpos(self, key, value):
        values = self.lists.get(key, [])
        return values.index(value) if value in values else None

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lpop(self, key):
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    def rpop(self, key):
        values = self.lists.get(key, [])
        return values.pop() if values else None

    def lmove(self, source, destination, wherefrom, whereto):
        assert wherefrom == "LEFT"
        assert whereto == "RIGHT"
        value = self.lpop(source)
        if value is not None:
            self.rpush(destination, value)
        return value

    def lrem(self, key, count, value):
        values = self.lists.get(key, [])
        removed = 0
        retained = []
        for candidate in values:
            if candidate == value and (count == 0 or removed < count):
                removed += 1
            else:
                retained.append(candidate)
        self.lists[key] = retained
        return removed

    def lpush(self, key, *values):
        target = self.lists.setdefault(key, [])
        for value in values:
            target.insert(0, value)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)

    def zscore(self, key, value):
        return self.scores.get(key, {}).get(value)

    def rpoplpush(self, source, destination):
        values = self.lists.get(source, [])
        if not values:
            return None
        value = values.pop()
        self.lists.setdefault(destination, []).insert(0, value)
        return value

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def sadd(self, key, value):
        values = self.sets.setdefault(key, set())
        if value in values:
            return 0
        values.add(value)
        return 1


def test_rebuild_accepted_ticker_index_excludes_provisional_records(monkeypatch):
    client = MagicMock()
    client.zrange.return_value = [("accepted", 200.0), ("provisional", 300.0)]
    client.hmget.return_value = [
        json.dumps({
            "article_verification_status": "canonical",
            "relevance_confidence": 0.93,
            "entity_link_version": 2,
            "relevance_label": "business",
            "related_tickers": ["NVDA", "LAZR"],
        }),
        json.dumps({
            "article_verification_status": "unverified",
            "relevance_confidence": 0.99,
            "entity_link_version": 2,
            "relevance_label": "business",
            "related_tickers": ["WRONG"],
        }),
    ]
    monkeypatch.setattr(calendar_relevance_worker, "r", client)

    count = calendar_relevance_worker._rebuild_accepted_ticker_index()

    assert count == 2
    pipe = client.pipeline.return_value
    pipe.delete.assert_called_once_with(calendar_relevance_queue.ACCEPTED_TICKERS_KEY)
    pipe.zadd.assert_called_once_with(
        calendar_relevance_queue.ACCEPTED_TICKERS_KEY,
        {"NVDA": 200.0, "LAZR": 200.0},
    )
    pipe.execute.assert_called_once()


def test_live_queue_is_consumed_before_backlog(monkeypatch):
    client = FakeRedis()
    client.rpush(calendar_relevance_queue.BACKLOG_PENDING_KEY, json.dumps({"id": "old"}))
    client.rpush(calendar_relevance_queue.LIVE_PENDING_KEY, json.dumps({"id": "new"}))
    monkeypatch.setattr(calendar_relevance_worker, "r", client)
    monkeypatch.setattr(calendar_relevance_worker, "BATCH_SIZE", 8)

    items, queue_name = calendar_relevance_worker._take_batch()

    assert queue_name == "live"
    assert [item["id"] for item in items] == ["new"]
    assert client.llen(calendar_relevance_queue.BACKLOG_PENDING_KEY) == 1


def test_worker_falls_back_to_historical_queue(monkeypatch):
    client = FakeRedis()
    client.rpush(calendar_relevance_queue.BACKLOG_PENDING_KEY, json.dumps({"id": "old"}))
    monkeypatch.setattr(calendar_relevance_worker, "r", client)
    monkeypatch.setattr(calendar_relevance_worker, "BATCH_SIZE", 8)

    items, queue_name = calendar_relevance_worker._take_batch()

    assert queue_name == "backlog"
    assert [item["id"] for item in items] == ["old"]


def test_take_batch_honors_configured_batch_size(monkeypatch):
    client = FakeRedis()
    for event_id in ("first", "second", "third"):
        client.rpush(
            calendar_relevance_queue.LIVE_PENDING_KEY,
            json.dumps({"id": event_id}),
        )
    monkeypatch.setattr(calendar_relevance_worker, "r", client)
    monkeypatch.setattr(calendar_relevance_worker, "BATCH_SIZE", 2)

    items, queue_name = calendar_relevance_worker._take_batch()

    assert queue_name == "live"
    assert [item["id"] for item in items] == ["first", "second"]
    assert client.llen(calendar_relevance_queue.LIVE_PENDING_KEY) == 1
    assert client.llen(calendar_relevance_queue.LIVE_PROCESSING_KEY) == 2


def test_interrupted_batch_is_recovered_in_original_order():
    client = FakeRedis()
    first = json.dumps({"id": "first"})
    second = json.dumps({"id": "second"})
    client.rpush(calendar_relevance_queue.LIVE_PROCESSING_KEY, first)
    client.rpush(calendar_relevance_queue.LIVE_PROCESSING_KEY, second)

    recovered = calendar_relevance_queue.recover_processing(client)

    assert recovered == 2
    assert client.lists[calendar_relevance_queue.LIVE_PENDING_KEY] == [first, second]
    assert client.llen(calendar_relevance_queue.LIVE_PROCESSING_KEY) == 0


def test_legacy_queue_migration_preserves_oldest_first_order():
    client = FakeRedis()
    for event_id in ("oldest", "middle", "newest"):
        client.rpush(calendar_relevance_queue.LEGACY_PENDING_KEY, json.dumps({"id": event_id}))

    moved = calendar_relevance_queue.migrate_legacy_pending(client)

    assert moved == 3
    assert [
        json.loads(value)["id"]
        for value in client.lists[calendar_relevance_queue.BACKLOG_PENDING_KEY]
    ] == ["oldest", "middle", "newest"]


def test_recent_backlog_items_are_promoted_without_reordering_old_items():
    client = FakeRedis()
    for event_id in ("oldest", "recent", "newest", "older"):
        client.rpush(
            calendar_relevance_queue.BACKLOG_PENDING_KEY,
            json.dumps({"id": event_id}),
        )
    client.scores[calendar_relevance_queue.RECENT_ARTICLES_KEY] = {
        "recent": 99_900,
        "newest": 99_950,
    }

    promoted = calendar_relevance_queue.promote_recent_backlog(client, now=100_000)

    assert promoted == 2
    assert [
        json.loads(value)["id"]
        for value in client.lists[calendar_relevance_queue.LIVE_PENDING_KEY]
    ] == ["newest", "recent"]
    assert [
        json.loads(value)["id"]
        for value in client.lists[calendar_relevance_queue.BACKLOG_PENDING_KEY]
    ] == ["oldest", "older"]


def test_classification_failure_is_split_and_isolated(monkeypatch):
    calls = []

    def classify(items):
        calls.append([item["id"] for item in items])
        if len(items) > 1 or items[0]["id"] == "bad":
            raise ValueError("truncated JSON")
        return {items[0]["id"]: {"relevance_label": "finance"}}

    monkeypatch.setattr(calendar_relevance_worker, "classify_articles", classify)

    results, failures = calendar_relevance_worker._classify_resilient([
        {"id": "good"},
        {"id": "bad"},
    ])

    assert results == {"good": {"relevance_label": "finance"}}
    assert failures[0][0]["id"] == "bad"
    assert calls == [["good", "bad"], ["good"], ["bad"]]


def test_provider_timeout_is_requeued_by_outer_worker_instead_of_isolated(monkeypatch):
    monkeypatch.setattr(
        calendar_relevance_worker,
        "classify_articles",
        lambda items: (_ for _ in ()).throw(requests.ReadTimeout("provider busy")),
    )

    try:
        calendar_relevance_worker._classify_resilient([{"id": "article"}])
    except requests.ReadTimeout:
        pass
    else:
        raise AssertionError("provider timeout should propagate")


def test_missing_batch_results_are_retried_separately(monkeypatch):
    calls = []

    def classify(items):
        calls.append([item["id"] for item in items])
        if len(items) > 1:
            return {"first": {"relevance_label": "finance"}}
        return {items[0]["id"]: {"relevance_label": "business"}}

    monkeypatch.setattr(calendar_relevance_worker, "classify_articles", classify)

    results, failures = calendar_relevance_worker._classify_resilient([
        {"id": "first"},
        {"id": "second"},
    ])

    assert set(results) == {"first", "second"}
    assert failures == []
    assert calls == [["first", "second"], ["second"]]


def test_fully_omitted_batch_is_split_instead_of_retried_forever(monkeypatch):
    calls = []

    def classify(items):
        calls.append([item["id"] for item in items])
        return {}

    monkeypatch.setattr(calendar_relevance_worker, "classify_articles", classify)
    items = [{"id": f"item-{index}"} for index in range(4)]

    results, failures = calendar_relevance_worker._classify_resilient(items)

    assert results == {}
    assert [item["id"] for item, _error in failures] == [
        "item-0",
        "item-1",
        "item-2",
        "item-3",
    ]
    assert calls == [
        ["item-0", "item-1", "item-2", "item-3"],
        ["item-0", "item-1"],
        ["item-0"],
        ["item-1"],
        ["item-2", "item-3"],
        ["item-2"],
        ["item-3"],
    ]


def test_repeated_single_item_failure_moves_to_failed_queue(monkeypatch):
    client = FakeRedis()
    client.sets[calendar_relevance_queue.QUEUED_KEY] = {"bad"}
    monkeypatch.setattr(calendar_relevance_worker, "r", client)
    monkeypatch.setattr(calendar_relevance_worker, "MAX_ITEM_ATTEMPTS", 3)

    failed = calendar_relevance_worker._handle_failed([
        ({"id": "bad", "_llm_attempts": 2}, "invalid JSON"),
    ], "backlog")

    assert failed == 1
    assert client.llen(calendar_relevance_queue.FAILED_KEY) == 1
    assert "bad" not in client.sets[calendar_relevance_queue.QUEUED_KEY]


def test_failed_items_can_be_replayed_without_stale_attempt_metadata():
    client = FakeRedis()
    client.rpush(
        calendar_relevance_queue.FAILED_KEY,
        json.dumps({
            "id": "recent",
            "title": "Recent article",
            "_queue": "live",
            "_llm_attempts": 3,
            "_llm_error": "timeout",
        }),
    )

    assert calendar_relevance_queue.replay_failed(client) == 1
    assert client.llen(calendar_relevance_queue.FAILED_KEY) == 0
    replayed = json.loads(client.lists[calendar_relevance_queue.LIVE_PENDING_KEY][0])
    assert replayed == {"id": "recent", "title": "Recent article"}
    assert "recent" in client.sets[calendar_relevance_queue.QUEUED_KEY]


def test_failed_replay_deduplicates_and_preserves_invalid_queue():
    client = FakeRedis()
    for item in [{"id": "a"}, {"id": "a"}, {"id": "active"},
                 {"id": "bad", "_queue": "invalid"}]:
        client.rpush(calendar_relevance_queue.FAILED_KEY, json.dumps(item))
    client.sadd(calendar_relevance_queue.QUEUED_KEY, "active")
    assert calendar_relevance_queue.replay_failed(client) == 1
    assert client.llen(calendar_relevance_queue.BACKLOG_PENDING_KEY) == 1
    assert json.loads(client.lists[calendar_relevance_queue.FAILED_KEY][0])["id"] == "bad"


def test_idle_worker_refills_unclassified_durable_articles(monkeypatch):
    client = FakeRedis()
    client.sets[calendar_relevance_queue.CLASSIFIED_KEY] = {"stale"}
    client.sets[calendar_relevance_queue.QUEUED_KEY] = {"stale"}
    client.rpush(
        calendar_relevance_queue.FAILED_KEY,
        json.dumps({"id": "failed"}),
    )
    events = [
        {"id": "stale", "title": "Needs enrichment", "date": "2026-07-25", "details": {}},
        {"id": "fresh", "title": "Also needs enrichment", "date": "2026-07-24", "details": {}},
        {"id": "failed", "title": "Already isolated", "date": "2026-07-23", "details": {}},
    ]
    monkeypatch.setattr(calendar_relevance_worker, "r", client)
    monkeypatch.setattr(calendar_relevance_worker.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(
        calendar_relevance_worker.persistent_store,
        "list_unclassified_market_news",
        lambda limit: events,
    )
    monkeypatch.setattr(
        calendar_relevance_worker.persistent_store,
        "list_market_news_needing_verification",
        lambda limit: [],
    )

    added = calendar_relevance_worker._refill_unclassified_backlog()

    assert added == 2
    assert [
        json.loads(raw)["id"]
        for raw in client.lists[calendar_relevance_queue.BACKLOG_PENDING_KEY]
    ] == ["stale", "fresh"]
    assert "failed" not in client.sets[calendar_relevance_queue.QUEUED_KEY]


def test_idle_worker_reprocesses_classified_articles_without_verification(monkeypatch):
    client = FakeRedis()
    events = [{
        "id": "classified-old",
        "title": "Previously classified article",
        "date": "2026-07-20",
        "details": {"relevance_label": "business"},
    }]
    monkeypatch.setattr(calendar_relevance_worker, "r", client)
    monkeypatch.setattr(calendar_relevance_worker.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(
        calendar_relevance_worker.persistent_store,
        "list_unclassified_market_news",
        lambda limit: [],
    )
    monkeypatch.setattr(
        calendar_relevance_worker.persistent_store,
        "list_market_news_needing_verification",
        lambda limit: events,
    )

    added = calendar_relevance_worker._refill_unclassified_backlog()

    assert added == 1
    queued = json.loads(client.lists[calendar_relevance_queue.BACKLOG_PENDING_KEY][0])
    assert queued["id"] == "classified-old"
def test_worker_supervisor_restarts_after_dependency_error(monkeypatch):
    calls = []
    sleeps = []

    def run_forever():
        calls.append(True)
        if len(calls) == 1:
            raise TimeoutError("Redis timed out")

    monkeypatch.setattr(calendar_relevance_worker, "run_forever", run_forever)
    monkeypatch.setattr(calendar_relevance_worker.time, "sleep", sleeps.append)

    calendar_relevance_worker.run_resilient()

    assert len(calls) == 2
    assert sleeps == [calendar_relevance_worker.SUPERVISOR_RETRY_SECONDS]
