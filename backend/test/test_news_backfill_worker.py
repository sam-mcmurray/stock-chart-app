from workers import news_backfill_worker
import pytest
import json
from unittest.mock import Mock


def test_heartbeat_retains_forward_sample_without_confusing_retry_timings(monkeypatch):
    client = Mock()
    monkeypatch.setattr(news_backfill_worker, "r", client)
    monkeypatch.setattr(news_backfill_worker, "_last_forward_batch", None)
    monkeypatch.setattr(news_backfill_worker.time, "time", lambda: 100)
    news_backfill_worker._heartbeat("processing_historical_articles")
    assert json.loads(client.set.call_args.args[1])["last_forward_batch"] is None
    news_backfill_worker._heartbeat("historical_articles_ok", inference_seconds=12.5, selection_seconds=0.002)
    sample = json.loads(client.set.call_args.args[1])["last_forward_batch"]
    for status in ("processing_historical_articles", "historical_retry_ok", "waiting_for_live_queue", "error"):
        news_backfill_worker._heartbeat(status, inference_seconds=99)
        assert json.loads(client.set.call_args.args[1])["last_forward_batch"] == sample
    assert sample["completed_at"] == 100
    assert sample["selection_seconds"] == 0.002


def test_forward_rate_smooths_batches_and_includes_retry_wait_time():
    rate = news_backfill_worker._ForwardProgressRate()
    assert rate.rate == 0
    assert rate.record(8, 0, 40) == 12
    # Sixty seconds of intervening retry/live work is not counted as progress.
    assert rate.record(8, 100, 140) == pytest.approx(16 * 60 / 140)
    assert len(rate.samples) == 2
    assert rate.record(0, 140, 150) == pytest.approx(16 * 60 / 140)


def test_forward_rate_window_is_bounded_and_elapsed_weighted():
    rate = news_backfill_worker._ForwardProgressRate()
    rate.record(1, 0, 1000)
    for index in range(20):
        rate.record(8, 1000 + index * 40, 1040 + index * 40)
    assert len(rate.samples) == 20
    assert rate.rate == 12


def test_historical_progress_keeps_fixed_corpus_total_during_retries():
    progress = news_backfill_worker._historical_progress(
        120,
        80,
        rate=20,
        cursor_published_ts=100,
    )

    assert progress == {
        "historical_total": 120,
        "historical_remaining": 80,
        "historical_eta_seconds": 240,
        "historical_cursor_published_ts": 100,
        "historical_rate_per_minute": 20.0,
    }


def test_calendar_item_preserves_canonical_and_mention_fallback_inputs():
    item = news_backfill_worker._calendar_item({
        "id": "article-1",
        "title": "Publisher headline",
        "source": "publisher.test",
        "url": "https://publisher.test/story",
        "symbols": ["TEST"],
        "details": {
            "summary": "Summary",
            "source_urls": ["https://publisher.test/story"],
            "event_ids": ["123"],
            "published_ts": 100,
        },
    })

    assert item["url"] == "https://publisher.test/story"
    assert item["event_ids"] == ["123"]
    assert item["source_urls"] == ["https://publisher.test/story"]


@pytest.mark.parametrize("with_retry", [False, True])
def test_one_shot_worker_transitions_from_calendar_to_history_and_completes(monkeypatch, with_retry):
    saved = []
    monkeypatch.setattr(news_backfill_worker, "ensure_historical_article_index", lambda: None)
    heartbeats = []
    historical_queries = []
    calendar_pages = [[{"id": "calendar-1"}], []]
    historical_pages = [[{"url": "https://publisher.test/old", "published_ts": 100}], []]
    retry_pages = [[{"url": "retry"}], []] if with_retry else [[]]
    monkeypatch.setattr(news_backfill_worker, "PROCESS_RETRIES", with_retry)
    monkeypatch.setattr(news_backfill_worker, "RETRY_EVERY_BATCHES", 1)

    monkeypatch.setattr(news_backfill_worker, "configured", lambda: True)
    monkeypatch.setattr(news_backfill_worker, "_heartbeat", lambda status, **values: heartbeats.append((status, values)))
    monkeypatch.setattr(news_backfill_worker.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        news_backfill_worker.persistent_store,
        "load_worker_checkpoint",
        lambda _name: {},
    )
    monkeypatch.setattr(
        news_backfill_worker.persistent_store,
        "save_worker_checkpoint",
        lambda name, value: saved.append((name, value.copy())),
    )
    checkpoint_groups = []

    def save_checkpoints(values):
        checkpoint_groups.append(values)
        saved.extend((name, value.copy()) for name, value in values.items())

    monkeypatch.setattr(news_backfill_worker.persistent_store, "save_worker_checkpoints", save_checkpoints)
    monkeypatch.setattr(
        news_backfill_worker.persistent_store,
        "list_market_news_backfill_page",
        lambda **_values: calendar_pages.pop(0),
    )
    def historical_page(**values):
        historical_queries.append(values)
        return historical_pages.pop(0)

    monkeypatch.setattr(news_backfill_worker, "list_historical_articles_newest", historical_page)
    monkeypatch.setattr(news_backfill_worker, "MIN_PUBLISHED_TS", 50)
    monkeypatch.setattr(news_backfill_worker, "newest_historical_article_timestamp", lambda: 100)
    monkeypatch.setattr(
        news_backfill_worker,
        "historical_article_partition_size",
        lambda **_values: 1,
    )
    monkeypatch.setattr(
        news_backfill_worker.persistent_store,
        "seed_historical_article_retries",
        lambda _version: 0,
    )
    monkeypatch.setattr(
        news_backfill_worker.persistent_store,
        "count_historical_article_retries",
        lambda: 0,
    )
    monkeypatch.setattr(
        news_backfill_worker.persistent_store,
        "list_historical_article_retries",
        lambda **_values: retry_pages.pop(0) if retry_pages else [],
    )
    monkeypatch.setattr(
        news_backfill_worker.persistent_store,
        "existing_historical_article_enrichment_urls",
        lambda _urls: set(),
    )
    monkeypatch.setattr(
        news_backfill_worker,
        "process_calendar_batch",
        lambda _rows: {"processed": 1, "removed": 0, "failed": 0},
    )
    monkeypatch.setattr(
        news_backfill_worker,
        "process_batch",
        lambda _rows: {"processed": 1, "failed": 0,
                       "completed_delta": 0 if _rows[0]["url"] == "retry" else 1,
                       "records_per_minute": 0.5 if _rows[0]["url"] == "retry" else 20},
    )

    news_backfill_worker.run()

    assert len(checkpoint_groups) == 1
    assert set(checkpoint_groups[0]) == {
        news_backfill_worker.CHECKPOINT_NAME, news_backfill_worker.HISTORICAL_CHECKPOINT_NAME,
    }
    forward = next(h for status, h in heartbeats if status == "historical_articles_ok")
    for key in ("selection_seconds", "existing_lookup_seconds", "checkpoint_seconds", "forward_seconds"):
        assert forward[key] >= 0
    assert heartbeats[-1][0] == "complete"
    assert heartbeats[-1][1]["calendar_completed"] == 1
    assert heartbeats[-1][1]["historical_completed"] == 1
    assert heartbeats[-1][1]["historical_remaining"] == 0
    assert heartbeats[-1][1]["historical_cursor_published_ts"] == 100
    assert saved[-1][1]["phase"] == "complete"
    assert saved[-1][1]["historical_start_published_ts"] == 100
    assert saved[-1][1]["historical_before_published_ts"] == 100
    assert all(query["min_published_ts"] == 50 for query in historical_queries)
    if with_retry:
        forward = next(h for status, h in heartbeats if status == "historical_articles_ok")
        retry = next(h for status, h in heartbeats if status == "historical_retry_ok")
        assert retry["historical_rate_per_minute"] == forward["historical_rate_per_minute"]
        assert retry["retry_records_per_minute"] == 0.5
        assert "records_per_minute" not in retry


def test_resilient_worker_retries_from_checkpoint_after_runtime_error(monkeypatch):
    calls = []
    sleeps = []

    def run():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("database restarted")

    monkeypatch.setattr(news_backfill_worker, "run", run)
    monkeypatch.setattr(news_backfill_worker, "_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(news_backfill_worker.time, "sleep", sleeps.append)

    news_backfill_worker.run_resilient()

    assert len(calls) == 2
    assert sleeps == [news_backfill_worker.MODEL_WAIT_SECONDS]
