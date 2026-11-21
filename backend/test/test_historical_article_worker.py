from datetime import datetime
from zoneinfo import ZoneInfo

from threading import Event
import pytest

from workers import historical_article_worker


@pytest.fixture(autouse=True)
def isolate_delivery_store(monkeypatch):
    monkeypatch.setattr(historical_article_worker.persistent_store, "existing_historical_article_enrichment_urls", lambda urls: set())
    monkeypatch.setattr(historical_article_worker.persistent_store, "recover_historical_article_results", lambda urls: None)
    monkeypatch.setattr(historical_article_worker.persistent_store, "historical_graph_backlog_full", lambda: False)


def test_historical_article_item_keeps_canonical_url_and_event_fallback_ids():
    item = historical_article_worker._article_item({
        "url": "https://publisher.test/story",
        "title": "derived historical title",
        "summary": "Event summary",
        "source": "publisher.test",
        "provider": "gdelt historical",
        "event_ids": ["123", "456"],
        "published_ts": 100,
    })

    assert item["url"] == "https://publisher.test/story"
    assert item["event_ids"] == ["123", "456"]
    assert item["title_inferred"] is True
    assert len(item["id"]) == 40


def test_historical_worker_runs_on_weekends_and_waits_during_market_day(monkeypatch):
    monkeypatch.setattr(historical_article_worker, "OFF_HOURS_ONLY", True)
    zone = ZoneInfo("America/Chicago")

    assert historical_article_worker._off_hours(datetime(2026, 7, 25, 12, tzinfo=zone))
    assert not historical_article_worker._off_hours(datetime(2026, 7, 24, 12, tzinfo=zone))


def test_processing_failure_is_queued_without_overwriting_enrichment(monkeypatch):
    queued = []
    monkeypatch.setattr(
        historical_article_worker,
        "resolve_article_batch",
        lambda items, **_kwargs: items,
    )
    monkeypatch.setattr(
        historical_article_worker,
        "classify_articles",
        lambda _items: (_ for _ in ()).throw(TimeoutError("model timed out")),
    )
    monkeypatch.setattr(
        historical_article_worker.persistent_store,
        "enqueue_historical_article_retry",
        lambda article, version, error: queued.append((article, version, error)),
    )
    monkeypatch.setattr(
        historical_article_worker.persistent_store,
        "save_historical_article_results",
        lambda *_args: (_ for _ in ()).throw(AssertionError("failure must not be persisted as enrichment")),
    )

    stats = historical_article_worker.process_batch([{
        "url": "https://publisher.test/timed-out",
        "title": "Timed out article",
        "published_ts": 100,
    }])

    assert stats["scanned"] == 1
    assert stats["processed"] == 0
    assert stats["failed"] == 1
    assert stats["completed_delta"] == 0
    assert stats["duration_seconds"] >= 0
    assert stats["records_per_minute"] > 0
    assert queued[0][0]["url"] == "https://publisher.test/timed-out"
    assert "timed out" in queued[0][2]


def test_successful_retry_replaces_failure_and_clears_queue(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        historical_article_worker,
        "resolve_article_batch",
        lambda items, **_kwargs: items,
    )

    def classify(items):
        return {
            items[0]["id"]: {
                "relevance_label": "finance",
                "relevance_source": "ollama",
                "display_headline": "Verified headline",
            }
        }

    monkeypatch.setattr(historical_article_worker, "classify_articles", classify)
    monkeypatch.setattr(
        historical_article_worker.persistent_store,
        "save_historical_article_results",
        lambda rows, version: resolved.extend(article["url"] for article, _ in rows),
    )

    stats = historical_article_worker.process_batch([{
        "url": "https://publisher.test/retry",
        "title": "Retry article",
        "published_ts": 100,
        "_checkpoint_counted": False,
    }])

    assert stats["processed"] == 1
    assert stats["failed"] == 0
    assert stats["completed_delta"] == 1
    assert resolved == ["https://publisher.test/retry"]


def test_large_batch_resolves_next_group_during_current_inference(monkeypatch):
    second_group_started = Event()
    persisted = []

    def resolve(items, **_kwargs):
        if items[0]["url"].endswith("/4"):
            second_group_started.set()
        return items

    def classify(items):
        if items[0]["url"].endswith("/0"):
            assert second_group_started.wait(timeout=1)
        return {
            item["id"]: {
                "relevance_label": "finance",
                "relevance_source": "ollama",
                "display_headline": item["title"],
            }
            for item in items
        }

    monkeypatch.setattr(historical_article_worker, "resolve_article_batch", resolve)
    monkeypatch.setattr(historical_article_worker, "classify_articles", classify)
    monkeypatch.setattr(
        historical_article_worker.persistent_store,
        "save_historical_article_results",
        lambda rows, version: persisted.extend(article["url"] for article, _ in rows),
    )

    stats = historical_article_worker.process_batch([
        {
            "url": f"https://publisher.test/{index}",
            "title": f"Article {index}",
            "published_ts": 100 - index,
        }
        for index in range(8)
    ])

    assert stats["processed"] == 8
    assert stats["failed"] == 0
    assert stats["pipelined_groups"] == 2
    assert len(persisted) == 8


def test_saved_result_recovery_never_resolves_or_calls_model(monkeypatch):
    store = historical_article_worker.persistent_store
    monkeypatch.setattr(store, "existing_historical_article_enrichment_urls", lambda urls: set(urls))
    recovered = []
    monkeypatch.setattr(store, "recover_historical_article_results", recovered.extend)
    monkeypatch.setattr(historical_article_worker, "resolve_article_batch", lambda *a, **k: pytest.fail("must reuse saved result"))
    monkeypatch.setattr(historical_article_worker, "classify_articles", lambda *a: pytest.fail("must not repeat inference"))
    stats = historical_article_worker.process_batch([{"url": "saved"}])
    assert stats["reused_results"] == stats["processed"] == 1
    assert recovered == ["saved"]


def test_full_delivery_queue_defers_new_inference(monkeypatch):
    monkeypatch.setattr(historical_article_worker.persistent_store, "historical_graph_backlog_full", lambda: True)
    monkeypatch.setattr(historical_article_worker, "classify_articles", lambda *a: pytest.fail("queue full"))
    with pytest.raises(RuntimeError, match="backlog full"):
        historical_article_worker.process_batch([{"url": "new"}])


def test_resilient_classification_retries_only_omitted_items(monkeypatch):
    calls = []

    def classify(items):
        calls.append([item["id"] for item in items])
        if len(items) > 1:
            return {items[0]["id"]: {"relevance_label": "finance"}}
        return {items[0]["id"]: {"relevance_label": "business"}}

    monkeypatch.setattr(historical_article_worker, "classify_articles", classify)
    items = [{"id": "first"}, {"id": "second"}]

    results, failures = historical_article_worker._classify_resilient(items)

    assert set(results) == {"first", "second"}
    assert failures == []
    assert calls == [["first", "second"], ["second"]]
