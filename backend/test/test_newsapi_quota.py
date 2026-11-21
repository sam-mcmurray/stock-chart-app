import os
import sys
from unittest.mock import MagicMock

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from news_sources.gdelt_source import GDELTRateLimitError
from workers import news_worker


def test_newsapi_disabled_by_default(monkeypatch):
    monkeypatch.setattr(news_worker, "NEWSAPI_ENABLED", False)

    assert news_worker._newsapi_allowed() is False


def test_newsapi_respects_min_interval(monkeypatch):
    redis_mock = MagicMock()
    redis_mock.get.side_effect = lambda key: "1000" if key == news_worker.K_NEWSAPI_LAST_FETCH else None
    monkeypatch.setattr(news_worker, "r", redis_mock)
    monkeypatch.setattr(news_worker, "NEWSAPI_ENABLED", True)
    monkeypatch.setattr(news_worker, "NEWSAPI_MIN_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(news_worker, "_now_ts", lambda: 1200)

    assert news_worker._newsapi_allowed() is False


def test_newsapi_respects_daily_limit(monkeypatch):
    redis_mock = MagicMock()
    redis_mock.get.side_effect = lambda key: "90" if key.startswith("newsapi:daily_count:") else None
    monkeypatch.setattr(news_worker, "r", redis_mock)
    monkeypatch.setattr(news_worker, "NEWSAPI_ENABLED", True)
    monkeypatch.setattr(news_worker, "NEWSAPI_DAILY_REQUEST_LIMIT", 90)

    assert news_worker._newsapi_allowed() is False


def test_newsapi_remaining_budget(monkeypatch):
    redis_mock = MagicMock()
    redis_mock.get.side_effect = lambda key: "87" if key.startswith("newsapi:daily_count:") else None
    monkeypatch.setattr(news_worker, "r", redis_mock)
    monkeypatch.setattr(news_worker, "NEWSAPI_ENABLED", True)
    monkeypatch.setattr(news_worker, "NEWSAPI_DAILY_REQUEST_LIMIT", 90)

    assert news_worker._newsapi_remaining_budget() == 3


def test_article_exists_checks_deduped_url(monkeypatch):
    redis_mock = MagicMock()
    redis_mock.exists.return_value = 1
    monkeypatch.setattr(news_worker, "r", redis_mock)

    assert news_worker._article_exists("https://example.com/article") is True


def test_interval_allowed_waits_for_min_interval(monkeypatch):
    redis_mock = MagicMock()
    redis_mock.get.return_value = "1000"
    monkeypatch.setattr(news_worker, "r", redis_mock)
    monkeypatch.setattr(news_worker, "_now_ts", lambda: 1100)

    assert news_worker._interval_allowed("source:last", 900) is False


def test_finnhub_news_respects_its_own_interval(monkeypatch):
    monkeypatch.setattr(news_worker, "FINNHUB_NEWS_ENABLED", True)
    monkeypatch.setattr(news_worker, "FINNHUB_NEWS_MIN_INTERVAL_SECONDS", 900)
    monkeypatch.setattr(news_worker, "_interval_allowed", lambda key, interval, enabled: False)
    loader = MagicMock()
    monkeypatch.setattr(news_worker, "load_finnhub_general_news", loader)

    assert news_worker._fetch_finnhub_current() == []
    loader.assert_not_called()


def test_gdelt_backfill_formats_datetime():
    assert news_worker._gdelt_datetime(1780315200) == "20260601120000"


def test_gdelt_allowed_respects_rate_limit_backoff(monkeypatch):
    redis_mock = MagicMock()
    redis_mock.get.side_effect = lambda key: "1000" if key == news_worker.K_GDELT_RATE_LIMITED else None
    monkeypatch.setattr(news_worker, "r", redis_mock)
    monkeypatch.setattr(news_worker, "GDELT_ENABLED", True)
    monkeypatch.setattr(news_worker, "GDELT_RATE_LIMIT_BACKOFF_SECONDS", 3600)
    monkeypatch.setattr(news_worker, "_now_ts", lambda: 1200)

    assert news_worker._gdelt_allowed() is False


def test_gdelt_allowed_stops_during_bulk_import(monkeypatch):
    monkeypatch.setattr(news_worker, "GDELT_ENABLED", True)
    monkeypatch.setattr(news_worker, "GDELT_BULK_IMPORT_ACTIVE", True)

    assert news_worker._gdelt_allowed() is False


def test_gdelt_backfill_stops_on_rate_limit(monkeypatch):
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    monkeypatch.setattr(news_worker, "r", redis_mock)
    monkeypatch.setattr(news_worker, "GDELT_ENABLED", True)
    monkeypatch.setattr(news_worker, "GDELT_BACKFILL_ENABLED", True)
    monkeypatch.setattr(news_worker, "GDELT_BACKFILL_MAX_CHUNKS", 4)
    monkeypatch.setattr(news_worker, "GDELT_BACKFILL_CHUNK_HOURS", 6)
    monkeypatch.setattr(news_worker, "GDELT_RATE_LIMIT_BACKOFF_SECONDS", 3600)
    monkeypatch.setattr(news_worker, "_now_ts", lambda: 2000000)

    calls = {"count": 0}

    def rate_limited(*args, **kwargs):
        calls["count"] += 1
        raise GDELTRateLimitError("limited")

    monkeypatch.setattr(news_worker, "load_gdelt_articles", rate_limited)

    assert news_worker._fetch_gdelt_backfill() == []
    assert calls["count"] == 1
