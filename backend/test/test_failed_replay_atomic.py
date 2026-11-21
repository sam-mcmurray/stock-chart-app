import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import redis

from services import calendar_relevance_queue as queue
from workers import calendar_relevance_worker as worker


@pytest.fixture
def client(monkeypatch):
    if os.getenv("RUN_REDIS_INTEGRATION") != "1":
        pytest.skip("Isolated Redis replay integration tests")
    prefix = "test:failed-replay:" + uuid4().hex
    keys = []
    for name in ("FAILED_KEY", "QUEUED_KEY", "CLASSIFIED_KEY", "LIVE_PENDING_KEY", "BACKLOG_PENDING_KEY",
                 "LIVE_PROCESSING_KEY", "BACKLOG_PROCESSING_KEY"):
        key = prefix + ":" + name
        monkeypatch.setattr(queue, name, key)
        if hasattr(worker, name):
            monkeypatch.setattr(worker, name, key)
        keys.append(key)
    connection = redis.from_url(queue.REDIS_URL, decode_responses=True)
    staged_key = prefix + ":inflight"
    monkeypatch.setattr(worker, "INFLIGHT_RESULTS_KEY", staged_key)
    keys.append(staged_key)
    try:
        yield connection
    finally:
        connection.delete(*keys)
        connection.close()


def test_parallel_replay_skips_completed_and_active_articles(client):
    client.sadd(queue.CLASSIFIED_KEY, "done")
    client.sadd(queue.QUEUED_KEY, "active")
    for key in ["one", "one", "two", "done", "active"]:
        client.rpush(queue.FAILED_KEY, json.dumps({"id": key, "_queue": "live", "_llm_attempts": 3}))
    with ThreadPoolExecutor(max_workers=2) as executor:
        counts = list(executor.map(lambda _: queue.replay_failed(client), range(2)))
    assert sum(counts) == 2
    rows = [json.loads(raw) for raw in client.lrange(queue.LIVE_PENDING_KEY, 0, -1)]
    assert sorted(row["id"] for row in rows) == ["one", "two"]
    assert all("_llm_attempts" not in row for row in rows)
    assert client.llen(queue.FAILED_KEY) == 0


def test_filtered_replay_preserves_unrelated_failures_exactly(client):
    unrelated = json.dumps({"id": "translation", "_llm_error": "translation failed", "_llm_attempts": 3})
    target = json.dumps({"id": "gate", "_llm_error": "Invalid financial relevance gate response", "_llm_attempts": 3})
    client.rpush(queue.FAILED_KEY, unrelated, target)
    assert queue.replay_failed(client, error_filter="Invalid financial relevance gate response") == 1
    assert client.lrange(queue.FAILED_KEY, 0, -1) == [unrelated]
    queued = json.loads(client.lindex(queue.BACKLOG_PENDING_KEY, 0))
    assert queued["id"] == "gate"
    assert "_llm_error" not in queued
    assert queue.replay_failed(client, error_filter="Invalid financial relevance gate response") == 0


def test_failure_before_transaction_leaves_original_record(client, monkeypatch):
    raw = json.dumps({"id": "one", "_llm_attempts": 3})
    client.rpush(queue.FAILED_KEY, raw)
    original = client.pipeline

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)

        def fail(*args, **kwargs):
            raise redis.ConnectionError("interrupted before EXEC")

        pipe.execute = fail
        return pipe

    monkeypatch.setattr(client, "pipeline", pipeline)
    with pytest.raises(redis.ConnectionError):
        queue.replay_failed(client)
    assert client.lrange(queue.FAILED_KEY, 0, -1) == [raw]
    assert not client.sismember(queue.QUEUED_KEY, "one")
    assert client.llen(queue.BACKLOG_PENDING_KEY) == 0


def test_invalid_records_retained_and_wrong_destination_type_fails_before_pop(client):
    invalid = ["not json", "[]", "null", json.dumps({"id": "bad", "_queue": "wrong"})]
    client.rpush(queue.FAILED_KEY, *invalid)
    assert queue.replay_failed(client) == 0
    assert client.lrange(queue.FAILED_KEY, 0, -1) == invalid
    client.set(queue.BACKLOG_PENDING_KEY, "wrong type")
    with pytest.raises(ValueError, match="Unexpected Redis type"):
        queue.replay_failed(client)
    assert client.lrange(queue.FAILED_KEY, 0, -1) == invalid


def test_lost_response_after_commit_does_not_duplicate_on_replay(client, monkeypatch):
    client.rpush(queue.FAILED_KEY, json.dumps({"id": "one"}))
    original = client.pipeline

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def lost_response(*args, **kwargs):
            execute(*args, **kwargs)
            raise redis.ConnectionError("reply lost after EXEC")

        pipe.execute = lost_response
        return pipe

    monkeypatch.setattr(client, "pipeline", pipeline)
    with pytest.raises(redis.ConnectionError):
        queue.replay_failed(client)
    monkeypatch.setattr(client, "pipeline", original)
    assert queue.replay_failed(client) == 0
    assert client.llen(queue.BACKLOG_PENDING_KEY) == 1
    assert client.sismember(queue.QUEUED_KEY, "one")


def test_inference_survives_new_redis_client_without_new_model_call(client, monkeypatch):
    monkeypatch.setattr(worker, "r", client)
    monkeypatch.setattr(worker, "resolve_article_batch", lambda rows: rows)
    calls = []

    def classify(rows):
        calls.append(rows)
        return {"one": {"relevance_label": "business", "relevance_source": "llm"}}, []

    monkeypatch.setattr(worker, "_classify_resilient", classify)
    item = {"id": "one", "title": "A company announcement", "_queue_raw": "original"}
    _, expected, _ = worker._prepare_inference([item])
    with redis.from_url(queue.REDIS_URL, decode_responses=True) as second_client:
        monkeypatch.setattr(worker, "r", second_client)
        recovered, results, failures = worker._prepare_inference([{**item, "_queue_raw": "recovered"}])
    assert results == expected
    assert failures == []
    assert recovered[0]["_queue_raw"] == "recovered"
    assert len(calls) == 1


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("committed", [False, True])
def test_processing_move_survives_interruption_without_loss_or_duplicate(client, monkeypatch, terminal, committed):
    monkeypatch.setattr(worker, "r", client)
    raw = json.dumps({"id": "one"})
    item = {"id": "one", "_queue_raw": raw}
    destination = queue.FAILED_KEY if terminal else queue.LIVE_PENDING_KEY
    client.rpush(queue.LIVE_PROCESSING_KEY, raw)
    client.sadd(queue.QUEUED_KEY, "one")
    original = client.pipeline

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def interrupted(*args, **kwargs):
            if committed:
                execute(*args, **kwargs)
            raise redis.ConnectionError("simulated interruption")

        pipe.execute = interrupted
        return pipe

    monkeypatch.setattr(client, "pipeline", pipeline)
    with pytest.raises(redis.ConnectionError):
        worker._move_processing_item(item, "live", destination, raw, terminal=terminal)
    assert client.llen(queue.LIVE_PROCESSING_KEY) == (0 if committed else 1)
    assert client.llen(destination) == (1 if committed else 0)
    monkeypatch.setattr(client, "pipeline", original)
    assert worker._move_processing_item(item, "live", destination, raw, terminal=terminal) is (not committed)
    assert client.llen(destination) == 1
    assert client.llen(queue.LIVE_PROCESSING_KEY) == 0
    assert bool(client.sismember(queue.QUEUED_KEY, "one")) is (not terminal)


def test_wrong_destination_type_keeps_processing_record(client, monkeypatch):
    monkeypatch.setattr(worker, "r", client)
    raw = json.dumps({"id": "one"})
    client.rpush(queue.LIVE_PROCESSING_KEY, raw)
    client.set(queue.FAILED_KEY, "wrong type")
    with pytest.raises(ValueError, match="Unexpected Redis type"):
        worker._move_processing_item({"id": "one", "_queue_raw": raw}, "live", queue.FAILED_KEY, raw, terminal=True)
    assert client.lrange(queue.LIVE_PROCESSING_KEY, 0, -1) == [raw]
