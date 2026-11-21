import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routers import news_llm_api


def test_shared_queue_counts_pool_once_and_combines_worker_rates(monkeypatch):
    monkeypatch.setattr(news_llm_api.time, "time", lambda: 100)
    base = {"queue_mode": "shared_newest_first", "historical_completed": 10,
            "historical_remaining": 100, "shared_completed": 30,
            "historical_rate_per_minute": 10, "ts": 99}
    result = news_llm_api._historical_operations(base, {**base, "ts": 100,
                                                  "historical_remaining": 90})
    assert result["completed"] == 30
    assert result["remaining"] == 90
    assert result["total"] == 120
    assert result["rate_per_minute"] == 20
    assert result["parallel_eta_seconds"] is None
    assert result["sustained_rate_per_minute"] is None
    assert result["partition_boundary_ts"] is None


def test_shared_sustained_rate_includes_idle_wall_time(monkeypatch):
    now = [4600]
    monkeypatch.setattr(news_llm_api.time, "time", lambda: now[0])
    heartbeat = {"queue_mode": "shared_newest_first", "shared_completed": 630,
                 "shared_tracking_started_at": 1000, "shared_tracking_baseline": 30,
                 "historical_remaining": 9000, "historical_rate_per_minute": 999,
                 "ts": 4600}
    first = news_llm_api._historical_operations(heartbeat, {})
    assert first["measured_completions"] == 600
    assert first["sustained_rate_per_minute"] == 10
    assert first["tracking_elapsed_seconds"] == 3600
    now[0] += 3600
    second = news_llm_api._historical_operations(heartbeat, {})
    assert second["sustained_rate_per_minute"] == 5
    assert second["parallel_eta_seconds"] is None


def test_shared_migration_does_not_add_stale_partition_remaining(monkeypatch):
    monkeypatch.setattr(news_llm_api.time, "time", lambda: 100)
    result = news_llm_api._historical_operations({"queue_mode": "shared_newest_first",
        "historical_remaining": 90, "shared_completed": 30, "ts": 100},
        {"historical_remaining": 9000, "historical_rate_per_minute": 999})
    assert result["remaining"] == 90
    assert result["rate_per_minute"] == 0
    assert result["parallel_eta_seconds"] is None


def test_historical_zero_rate_does_not_fall_back_to_retry_attempt_rate():
    result = news_llm_api._historical_operations({
        "historical_remaining": 100, "historical_rate_per_minute": 0,
        "records_per_minute": 50,
    }, {})
    assert result["rate_per_minute"] == 0
    assert result["parallel_eta_seconds"] is None


def test_status_includes_historical_article_progress(monkeypatch):
    client = MagicMock()
    client.get.side_effect = [
        json.dumps({
            "status": "waiting_for_current_news",
            "completed": 1250,
            "current_db_work": 8356,
            "historical_remaining": 150000,
            "historical_eta_seconds": 750000,
            "historical_rate_per_minute": 12,
            "historical_cursor_published_ts": 1777593600,
        }),
        json.dumps({
            "status": "waiting_for_live_queue",
            "historical_completed": 320,
            "historical_remaining": 4300000,
            "records_per_minute": 10.5,
            "historical_cursor_published_ts": 1541721600,
        }),
        json.dumps({"status": "ok", "completed": 500, "remaining": 1000, "changed": 12}),
        json.dumps({"status": "ok", "records_per_minute": 12}),
    ]
    client.llen.return_value = 0
    monkeypatch.setattr(news_llm_api, "r", client)
    monkeypatch.setattr(
        news_llm_api,
        "pending_counts",
        lambda _client: {"live": 24, "backlog": 96, "processing": 2, "total": 122},
    )
    monkeypatch.setattr(
        news_llm_api,
        "server_status",
        lambda _client: {"available": True, "active_model": "qwen3.5:4b"},
    )

    result = news_llm_api.llm_status(_access_token="test")

    assert result["historical_backfill"]["status"] == "waiting_for_current_news"
    assert result["historical_backfill"]["completed"] == 1250
    assert result["historical_helper"]["status"] == "waiting_for_live_queue"
    assert result["historical_helper"]["historical_completed"] == 320
    assert result["historical_operations"]["completed"] == 1570
    assert result["historical_operations"]["remaining"] == 4450000
    assert result["historical_operations"]["rate_per_minute"] == 22.5
    assert result["historical_operations"]["parallel_eta_seconds"] == 24571429
    assert result["historical_operations"]["partition_boundary_ts"] == 1541721600
    assert result["entity_relink"]["completed"] == 500
    assert result["live_worker"]["pending"] == 122
    assert result["live_worker"]["eta_seconds"] == 610


def test_quality_returns_and_caches_persistent_metrics(monkeypatch):
    wall_times = iter([1000, 1090])
    monotonic_times = iter([10.0, 70.25, 90.75])
    monkeypatch.setattr(news_llm_api, "time", SimpleNamespace(
        time=lambda: next(wall_times), monotonic=lambda: next(monotonic_times),
    ))
    client = MagicMock()
    client.get.return_value = None
    monkeypatch.setattr(news_llm_api, "r", client)
    monkeypatch.setattr(
        news_llm_api.persistent_store,
        "news_llm_quality_summary",
        lambda: {"available": True, "completed": 120, "coverage": {"tickers_percent": 20.0}},
    )
    requested = []
    monkeypatch.setattr(
        news_llm_api.persistent_store,
        "news_llm_review_samples",
        lambda per_category: requested.append(per_category) or [{"category": "ticker", "url": "https://example.com/story"}],
    )

    result = news_llm_api.llm_quality(refresh=True, _access_token="test")

    assert result["summary"]["completed"] == 120
    assert result["samples"][0]["category"] == "ticker"
    assert result["cache"]["fresh_for_seconds"] == news_llm_api.QUALITY_CACHE_FRESH_SECONDS
    assert result["cache"]["generated_at"] == 1000
    assert result["cache"]["finished_at"] == 1090
    assert result["cache"]["report_version"] == "single-pass-v1"
    assert result["cache"]["summary_seconds"] == 60.25
    assert result["cache"]["samples_seconds"] == 20.5
    assert result["cache"]["refresh_seconds"] == 80.75
    assert requested == [5]
    client.setex.assert_called_once()
    assert client.setex.call_args.args[1] == news_llm_api.QUALITY_CACHE_STALE_SECONDS
    client.eval.assert_called_once()


def test_quality_uses_cached_report(monkeypatch):
    client = MagicMock()
    client.get.return_value = json.dumps({"summary": {"completed": 99}, "samples": []})
    monkeypatch.setattr(news_llm_api, "r", client)

    result = news_llm_api.llm_quality(refresh=False, _access_token="test")

    assert result["summary"]["completed"] == 99
    assert "report_version" not in result["cache"]


@pytest.mark.parametrize("cached", [None, {"summary": {"completed": 99}, "cache": {"generated_at": 1}}])
def test_quality_poll_never_starts_a_database_scan(monkeypatch, cached):
    client = MagicMock()
    client.get.return_value = json.dumps(cached) if cached else None
    client.exists.return_value = False
    monkeypatch.setattr(news_llm_api, "r", client)
    summary = MagicMock()
    samples = MagicMock()
    monkeypatch.setattr(news_llm_api.persistent_store, "news_llm_quality_summary", summary)
    monkeypatch.setattr(news_llm_api.persistent_store, "news_llm_review_samples", samples)
    result = news_llm_api.llm_quality(refresh=False, _access_token="test")
    assert result["cache"]["refresh_required"] is True
    assert result["cache"]["refresh_in_progress"] is False
    summary.assert_not_called()
    samples.assert_not_called()
    client.set.assert_not_called()
    if not cached:
        assert result["summary"]["available"] is False


def test_quality_returns_stale_cache_while_another_request_refreshes(monkeypatch):
    client = MagicMock()
    client.get.return_value = json.dumps({
        "summary": {"completed": 99},
        "samples": [],
        "cache": {"generated_at": 1},
    })
    client.set.return_value = False
    monkeypatch.setattr(news_llm_api, "r", client)
    summary = MagicMock()
    monkeypatch.setattr(
        news_llm_api.persistent_store,
        "news_llm_quality_summary",
        summary,
    )

    result = news_llm_api.llm_quality(refresh=False, _access_token="test")

    assert result["summary"]["completed"] == 99
    assert result["cache"]["refresh_in_progress"] is True
    summary.assert_not_called()


def test_quality_refresh_failure_falls_back_to_stale_cache(monkeypatch):
    client = MagicMock()
    client.get.return_value = json.dumps({
        "summary": {"completed": 99},
        "samples": [],
        "cache": {"generated_at": 1},
    })
    client.set.return_value = True
    monkeypatch.setattr(news_llm_api, "r", client)
    monkeypatch.setattr(
        news_llm_api.persistent_store,
        "news_llm_quality_summary",
        MagicMock(side_effect=TimeoutError("aggregate timed out")),
    )

    result = news_llm_api.llm_quality(refresh=True, _access_token="test")

    assert result["summary"]["completed"] == 99
    assert result["cache"]["refresh_failed"] is True
    client.eval.assert_called_once()


@pytest.mark.parametrize("has_cache", [True, False])
def test_quality_database_refresh_guard_after_redis_lease_expires(monkeypatch, has_cache):
    client = MagicMock()
    client.get.return_value = json.dumps({
        "summary": {"completed": 99}, "cache": {"generated_at": 1},
    }) if has_cache else None
    client.set.return_value = True
    monkeypatch.setattr(news_llm_api, "r", client)
    monkeypatch.setattr(
        news_llm_api.persistent_store, "news_llm_quality_summary",
        MagicMock(side_effect=news_llm_api.persistent_store.NewsQualityRefreshBusy()),
    )
    samples = MagicMock()
    monkeypatch.setattr(news_llm_api.persistent_store, "news_llm_review_samples", samples)

    if has_cache:
        result = news_llm_api.llm_quality(refresh=True, _access_token="test")
        assert result["summary"]["completed"] == 99
        assert result["cache"]["generated_at"] == 1
        assert result["cache"]["refresh_in_progress"] is True
        assert result["cache"]["refresh_failed"] is False
    else:
        with pytest.raises(news_llm_api.HTTPException) as exc:
            news_llm_api.llm_quality(refresh=True, _access_token="test")
        assert exc.value.status_code == 503
    samples.assert_not_called()
    client.setex.assert_not_called()
    client.eval.assert_called_once()


def test_quality_summary_skips_scans_when_database_lock_is_busy(monkeypatch):
    store = news_llm_api.persistent_store
    monkeypatch.setattr(store, "enabled", lambda: True)
    monkeypatch.setattr(store, "ensure_schema", lambda: None)
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {"acquired": False}
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(store.psycopg, "connect", connect)

    with pytest.raises(store.NewsQualityRefreshBusy):
        store.news_llm_quality_summary()

    connection.execute.assert_called_once_with(
        "SELECT pg_try_advisory_xact_lock(741852965) AS acquired"
    )
    assert connect.return_value.__exit__.call_args.args[0] is store.NewsQualityRefreshBusy


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="PostgreSQL lock test")
def test_quality_summary_database_lock_blocks_duplicate_and_releases(monkeypatch):
    store = news_llm_api.persistent_store
    monkeypatch.setattr(store, "ensure_schema", lambda: None)
    sql = "SELECT pg_try_advisory_xact_lock(741852965) AS acquired"
    with store.psycopg.connect(store.DATABASE_URL, row_factory=store.dict_row) as owner:
        assert owner.execute(sql).fetchone()["acquired"]
        with pytest.raises(store.NewsQualityRefreshBusy):
            store.news_llm_quality_summary()
        owner.rollback()
        with store.psycopg.connect(store.DATABASE_URL, row_factory=store.dict_row) as next_owner:
            assert next_owner.execute(sql).fetchone()["acquired"]


def test_training_export_is_bounded_authenticated_jsonl(monkeypatch):
    monkeypatch.setattr(news_llm_api.persistent_store, "enabled", lambda: True)

    response = news_llm_api.export_accepted_news_training_records(
        ticker="nvda",
        start_ts=100,
        end_ts=200,
        limit=500,
        _access_token="test",
    )

    assert response.media_type == "application/x-ndjson"
    assert response.headers["content-disposition"] == 'attachment; filename="accepted-news-NVDA.jsonl"'


def test_training_export_rejects_reversed_date_range(monkeypatch):
    monkeypatch.setattr(news_llm_api.persistent_store, "enabled", lambda: True)

    try:
        news_llm_api.export_accepted_news_training_records(
            ticker=None,
            start_ts=200,
            end_ts=100,
            limit=500,
            _access_token="test",
        )
    except news_llm_api.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("Reversed training export dates must be rejected")


def test_retry_failed_replays_bounded_records(monkeypatch):
    client = MagicMock()
    client.llen.return_value = 3
    monkeypatch.setattr(news_llm_api, "r", client)
    monkeypatch.setattr(news_llm_api, "replay_failed", lambda _client, limit: 12 if limit == 25 else 0)
    monkeypatch.setattr(
        news_llm_api,
        "pending_counts",
        lambda _client: {"live": 10, "backlog": 2, "processing": 0, "total": 12},
    )

    result = news_llm_api.retry_failed_news_llm_items(limit=25, _access_token="test")

    assert result == {
        "replayed": 12,
        "failed_remaining": 3,
        "live_pending": 10,
        "backlog_pending": 2,
    }


def test_review_quality_sample_preserves_stale_safe_cache(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(news_llm_api, "r", client)
    monkeypatch.setattr(
        news_llm_api.persistent_store,
        "review_news_llm_sample",
        lambda url, category, judgment, note: {
            "validation_key": "sample-key",
            "article_url": url,
            "category": category,
            "judgment": judgment,
            "note": note,
        },
    )

    result = news_llm_api.review_llm_quality_sample(
        news_llm_api.LlmSampleReview(
            article_url="https://example.com/story",
            category="ticker",
            judgment="correct",
        ),
        _access_token="test",
    )

    assert result["judgment"] == "correct"
    client.delete.assert_not_called()


def test_review_quality_sample_rejects_invalid_judgment(monkeypatch):
    monkeypatch.setattr(
        news_llm_api.persistent_store,
        "review_news_llm_sample",
        lambda *_args: (_ for _ in ()).throw(ValueError("Invalid News LLM validation category or judgment")),
    )

    try:
        news_llm_api.review_llm_quality_sample(
            news_llm_api.LlmSampleReview(
                article_url="https://example.com/story",
                category="ticker",
                judgment="maybe",
            ),
            _access_token="test",
        )
    except news_llm_api.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("Invalid judgments must be rejected")


def test_pull_pauses_selects_and_removes_other_models(monkeypatch):
    settings_updates = []
    monkeypatch.setattr(news_llm_api, "r", MagicMock())
    monkeypatch.setattr(
        news_llm_api,
        "update_settings",
        lambda _client, **values: settings_updates.append(values) or {
            "enabled": bool(values.get("enabled")),
            "active_model": values.get("active_model") or "",
        },
    )
    monkeypatch.setattr(
        news_llm_api,
        "pull_model",
        lambda model: {"model": model, "status": "success"},
    )
    monkeypatch.setattr(
        news_llm_api,
        "delete_unselected_models",
        lambda selected: ["old:1b"] if selected == "new:3b" else [],
    )

    result = news_llm_api.llm_pull(
        news_llm_api.ModelAction(model="new:3b"),
        _access_token="test",
    )

    assert settings_updates == [
        {"enabled": False},
        {"active_model": "new:3b", "enabled": False},
    ]
    assert result["active_model"] == "new:3b"
    assert result["enabled"] is False
    assert result["removed_models"] == ["old:1b"]


def test_enable_requires_an_installed_selected_model(monkeypatch):
    monkeypatch.setattr(news_llm_api, "r", MagicMock())
    monkeypatch.setattr(news_llm_api, "list_models", lambda: [])
    monkeypatch.setattr(
        news_llm_api,
        "get_settings",
        lambda _client: {"enabled": False, "active_model": ""},
    )

    try:
        news_llm_api.llm_settings(
            news_llm_api.LlmSettingsUpdate(enabled=True),
            _access_token="test",
        )
    except news_llm_api.HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("Enabling without a selected model should fail")


def test_watch_returns_current_worker_input_and_output(monkeypatch):
    client = MagicMock()
    client.get.return_value = json.dumps({
        "phase": "completed",
        "inputs": [{"id": "article-1", "title": "Company reports"}],
        "outputs": {"article-1": {"relevance_label": "business"}},
    })
    monkeypatch.setattr(news_llm_api, "r", client)

    result = news_llm_api.llm_watch(_access_token="test")

    assert result["phase"] == "completed"
    assert result["inputs"][0]["id"] == "article-1"
    assert result["outputs"]["article-1"]["relevance_label"] == "business"
