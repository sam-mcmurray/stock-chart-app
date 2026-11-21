import json
from unittest.mock import Mock

import pytest

from workers import calendar_relevance_worker as worker


@pytest.fixture
def cache(monkeypatch):
    entries = {}
    client = Mock()
    client.hmget.side_effect = lambda key, ids: [entries.get(i) for i in ids]
    client.hset.side_effect = lambda key, mapping: entries.update(mapping)
    monkeypatch.setattr(worker, "r", client)
    monkeypatch.setattr(worker, "known_rejections", lambda items: {})
    return entries


def test_projection_retry_and_restart_reuse_matching_inference(cache, monkeypatch):
    resolve = Mock(side_effect=lambda rows: [{**row, "title": "Resolved title"} for row in rows])
    classify = Mock(return_value=({"a": {"relevance_label": "business", "relevance_source": "llm"}}, []))
    monkeypatch.setattr(worker, "resolve_article_batch", resolve)
    monkeypatch.setattr(worker, "_classify_resilient", classify)
    original = {"id": "a", "url": "https://test/a", "title": "Original title", "_queue_raw": "first"}
    resolved, results, _ = worker._prepare_inference([original])
    assert json.loads(cache["a"])["result"] == results["a"]
    # A failed projection requeues resolved input; a restart recovers original input.
    for item in [resolved[0], original]:
        retry = {**item, "_queue_raw": "current", "_llm_attempts": 2}
        recovered, reused, failed = worker._prepare_inference([retry])
        assert recovered[0]["_queue_raw"] == "current"
        assert recovered[0]["title"] == "Resolved title"
        assert reused == results
        assert failed == []
    assert classify.call_count == resolve.call_count == 1


def test_rejected_url_never_refetches_or_reinfers_even_without_inflight_cache(cache, monkeypatch):
    result = {"relevance_label": "unrelated", "rejected_receipt": True}
    monkeypatch.setattr(worker, "known_rejections", lambda items: {"https://test/crime": result})
    resolve, classify = Mock(), Mock()
    monkeypatch.setattr(worker, "resolve_article_batch", resolve)
    monkeypatch.setattr(worker, "_classify_resilient", classify)
    item = {"id": "a", "url": "https://test/crime", "_queue_raw": "new-claim"}
    restored, results, failures = worker._prepare_inference([item])
    assert restored == [item]
    assert results == {"a": result}
    assert failures == []
    resolve.assert_not_called()
    classify.assert_not_called()


def test_changed_input_and_corrupt_cache_require_fresh_analysis(cache, monkeypatch):
    monkeypatch.setattr(worker, "resolve_article_batch", lambda rows: rows)
    classify = Mock(return_value=({"a": {"relevance_label": "business"}}, []))
    monkeypatch.setattr(worker, "_classify_resilient", classify)
    worker._prepare_inference([{"id": "a", "title": "first"}])
    worker._prepare_inference([{"id": "a", "title": "changed"}])
    cache["a"] = "broken json"
    worker._prepare_inference([{"id": "a", "title": "changed"}])
    assert classify.call_count == 3


def test_partial_results_are_staged_but_failures_are_not(cache, monkeypatch):
    items = [{"id": "a"}, {"id": "b"}]
    monkeypatch.setattr(worker, "resolve_article_batch", lambda rows: rows)
    monkeypatch.setattr(worker, "_classify_resilient", lambda rows: (
        {"a": {"relevance_label": "business"}}, [(items[1], "invalid translation")],
    ))
    _, _, failures = worker._prepare_inference(items)
    assert set(cache) == {"a"}
    assert failures == [(items[1], "invalid translation")]


def test_completed_items_are_not_requeued(monkeypatch):
    client = Mock()
    client.sismember.return_value = True
    monkeypatch.setattr(worker, "r", client)
    worker._requeue([{"id": "done", "_queue_raw": "raw"}], "live")
    client.rpush.assert_not_called()
    client.lrem.assert_called_once()
