# backend/test/test_scanners_api.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import json
from datetime import datetime, timezone

# Adjust path for imports
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Before importing the app, mock redis
mock_redis_instance = MagicMock()
with patch('redis.from_url', return_value=mock_redis_instance):
    from main import app # This should be your FastAPI app instance
    from routers import scanners_api
    from workers import scanner_worker as scanner_worker_module

client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_redis_mock():
    mock_redis_instance.reset_mock()
    mock_redis_instance.hgetall.side_effect = None
    mock_redis_instance.hgetall.return_value = {}
    mock_redis_instance.hget.side_effect = None
    mock_redis_instance.hget.return_value = None
    mock_redis_instance.hmget.side_effect = None
    mock_redis_instance.hmget.return_value = []
    mock_redis_instance.zrevrange.return_value = []
    mock_redis_instance.zrange.return_value = []
    mock_redis_instance.get.return_value = None
    mock_redis_instance.hlen.side_effect = None
    mock_redis_instance.hlen.return_value = 0
    mock_redis_instance.zcard.side_effect = None
    mock_redis_instance.zcard.return_value = 0
    mock_redis_instance.llen.side_effect = None
    mock_redis_instance.llen.return_value = 0
    with (
        patch.object(scanners_api, "r", mock_redis_instance),
        patch.object(scanners_api.persistent_store, "enabled", return_value=False),
        patch.object(scanner_worker_module, "r", mock_redis_instance),
        patch.object(scanner_worker_module.persistent_store, "enabled", return_value=False),
    ):
        yield

def test_scanner_focus_endpoint():
    # Mock the return value of r.hgetall
    mock_redis_instance.hgetall.return_value = {
        "AAPL": json.dumps({"symbol": "AAPL", "last": 150.0}),
        "GOOG": json.dumps({"symbol": "GOOG", "last": 2800.0}),
    }
    
    response = client.get("/scanner/focus")
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert any(item['symbol'] == 'AAPL' for item in data)
    assert any(item['symbol'] == 'GOOG' for item in data)
    mock_redis_instance.hgetall.assert_called_with("scanner:focus")

def test_scanner_focus_empty():
    mock_redis_instance.hgetall.return_value = {}
    
    response = client.get("/scanner/focus")
    
    assert response.status_code == 200
    assert response.json() == []


def test_attach_discovery_sources_explains_unknown_catalyst(monkeypatch):
    monkeypatch.setattr(
        scanners_api,
        "_scanner_discovery_sources",
        lambda _symbol: ["graph_partnership", "schwab_mover"],
    )
    payload = {"catalyst": {"primary": "unknown", "direction": "neutral"}}

    scanners_api._attach_discovery_sources(payload, "LAZR")

    assert payload["watch_sources"] == ["graph_partnership", "schwab_mover"]
    assert payload["catalyst"]["sources"] == [
        {
            "type": "graph_partnership",
            "category": "company_relation",
            "label": "Company relationship: partnership",
        },
        {
            "type": "schwab_mover",
            "category": "discovery",
            "label": "Schwab market movers",
        },
    ]


def test_news_quality_endpoint_returns_bounded_review_queue():
    report = {"run_id": 4, "issues_detected": 2}
    issues = [{"issue_key": "a" * 64, "issue_type": "missing_title", "status": "open"}]
    with (
        patch.object(scanners_api.persistent_store, "enabled", return_value=True),
        patch.object(scanners_api.persistent_store, "latest_news_quality_report", return_value=report),
        patch.object(scanners_api.persistent_store, "news_quality_issue_counts", return_value={"open": 1}),
        patch.object(scanners_api.persistent_store, "list_news_quality_issues", return_value=issues) as listing,
    ):
        response = client.get("/news/quality?status=open&limit=25")

    assert response.status_code == 200
    assert response.json()["report"] == report
    assert response.json()["issues"] == issues
    listing.assert_called_once_with("open", 25)


def test_review_news_quality_issue_rejects_invalid_key():
    with pytest.raises(scanners_api.HTTPException) as exc:
        scanners_api.review_news_quality_issue(
            "not-a-key",
            scanners_api.NewsQualityReview(status="dismissed"),
            _access_token="token",
        )

    assert exc.value.status_code == 400


def test_review_news_quality_issue_records_decision():
    issue_key = "b" * 64
    expected = {"issue_key": issue_key, "status": "reviewed"}
    with patch.object(
        scanners_api.persistent_store,
        "review_news_quality_issue",
        return_value=expected,
    ) as review:
        result = scanners_api.review_news_quality_issue(
            issue_key,
            scanners_api.NewsQualityReview(status="reviewed", note="Ticker confirmed"),
            _access_token="token",
        )

    assert result == expected
    review.assert_called_once_with(issue_key, "reviewed", "Ticker confirmed")


def test_scanner_high_rvol_endpoint():
    def hgetall_side_effect(key):
        if key == "scanner:settings":
            return {"rvol_threshold": "3.0"}
        if key == "scanner:focus":
            return {
                "AAPL": json.dumps({"symbol": "AAPL", "rvol": 4.2}),
                "MSFT": json.dumps({"symbol": "MSFT", "rvol": 2.8}),
                "NVDA": json.dumps({"symbol": "NVDA", "rvol": 6.1}),
            }
        return {}

    mock_redis_instance.hgetall.side_effect = hgetall_side_effect

    response = client.get("/scanner/high-rvol")

    assert response.status_code == 200
    data = response.json()
    assert data["min_rvol"] == 3.0
    assert data["count"] == 2
    assert [row["symbol"] for row in data["items"]] == ["NVDA", "AAPL"]

def test_scanner_watchlists_endpoint():
    def hgetall_side_effect(key):
        if key == "scanner:watchlist:actionable":
            return {
                "AAPL": json.dumps({"symbol": "AAPL", "score": 9.2}),
                "TSLA": json.dumps({"symbol": "TSLA", "score": 7.5}),
            }
        if key == "scanner:watchlist:priority":
            return {
                "NVDA": json.dumps({"symbol": "NVDA", "score": 8.1}),
            }
        return {}

    mock_redis_instance.hgetall.side_effect = hgetall_side_effect

    response = client.get("/scanner/watchlists")

    assert response.status_code == 200
    data = response.json()
    assert data["actionable"][0]["symbol"] == "AAPL"
    assert data["priority"][0]["symbol"] == "NVDA"
    assert "higher_float" in data
    assert "lower_volume" in data
    assert "too_expensive" in data

def test_scanner_schwab_quotes_batches_and_normalizes_prices():
    schwab_client = MagicMock()
    schwab_client.get_quotes.return_value = {
        "AAPL": {
            "realtime": True,
            "reference": {"description": "Apple Inc."},
            "quote": {
                "lastPrice": 213.45,
                "bidPrice": 213.44,
                "askPrice": 213.46,
                "mark": 213.45,
                "netChange": 2.45,
                "netPercentChange": 1.161,
                "closePrice": 211.0,
                "quoteTime": 1_800_000_000_000,
            },
        },
        "MSFT": {
            "realtime": True,
            "quote": {"lastPrice": 510.25},
        },
    }
    with (
        patch.object(scanners_api, "_mk_schwab_market_client", return_value=schwab_client),
        patch.object(scanners_api.time, "time", return_value=1_800_000_001),
    ):
        response = scanners_api.scanner_schwab_quotes("aapl,MSFT,AAPL", "token")

    payload = json.loads(response.body)
    schwab_client.get_quotes.assert_called_once_with(
        ["AAPL", "MSFT"], fields="quote,reference"
    )
    assert payload["requested_count"] == 2
    assert payload["quote_count"] == 2
    assert payload["quotes"]["AAPL"] == {
        "symbol": "AAPL",
        "description": "Apple Inc.",
        "last": 213.45,
        "bid": 213.44,
        "ask": 213.46,
        "mark": 213.45,
        "net_change": 2.45,
        "day_change_pct": 1.161,
        "previous_close": 211.0,
        "market_ts": 1_800_000_000,
        "fetched_ts": 1_800_000_001,
        "realtime": True,
        "source": "schwab",
    }


def test_scanner_context_returns_company_and_linked_news():
    articles = [{
        "id": "article-1",
        "title": "NVIDIA announces a partnership",
        "published_ts": 1_800_000_000,
        "article_url": "https://example.com/nvidia",
    }]
    with (
        patch.object(
            scanners_api,
            "_company_info",
            return_value={"name": "NVIDIA Corporation", "source": "security_master"},
        ),
        patch.object(scanners_api, "_recent_articles", return_value=articles),
    ):
        response = scanners_api.scanner_context("nvda", _access_token="token")

    payload = json.loads(response.body)
    assert payload["symbol"] == "NVDA"
    assert payload["company"]["name"] == "NVIDIA Corporation"
    assert payload["articles"] == articles
    assert payload["news_watch"]["article_count"] == 1


def test_scanner_context_rejects_invalid_symbol():
    with pytest.raises(scanners_api.HTTPException) as exc:
        scanners_api.scanner_context("bad symbol", _access_token="token")
    assert exc.value.status_code == 400

def test_scanner_settings_update():
    mock_redis_instance.hgetall.return_value = {
        "price_min": "0",
        "price_max": "20",
        "rvol_threshold": "3",
        "rvol_window_days": "60",
        "float_max": "20000000",
        "scan_interval": "30",
    }

    response = client.put("/scanner/settings", json={"price_max": 15, "rvol_threshold": 4})

    assert response.status_code == 200
    mock_redis_instance.hset.assert_called_with(
        "scanner:settings",
        mapping={"price_max": "15.0", "rvol_threshold": "4.0"},
    )

def test_scanner_settings_update_rejects_invalid_price_range():
    response = client.put("/scanner/settings", json={"price_min": 25, "price_max": 15})

    assert response.status_code == 400
    assert "price_min must be less than or equal to price_max" in response.text

def test_scanner_status_endpoint():
    counts = {
        "scanner:focus": 2,
        "scanner:watchlist:actionable": 1,
        "scanner:watchlist:priority": 3,
        "news:recent:tickers": 4,
    }

    def hlen_side_effect(key):
        return counts.get(key, 0)

    def zcard_side_effect(key):
        return counts.get(key, 0)

    mock_redis_instance.hlen.side_effect = hlen_side_effect
    mock_redis_instance.zcard.side_effect = zcard_side_effect
    mock_redis_instance.get.return_value = json.dumps({"status": "ok", "ts": 1700000000})

    with patch("routers.scanners_api.time.time", return_value=1700000060):
        response = client.get("/scanner/status")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["heartbeat"]["status"] == "ok"
    assert data["heartbeat_age_seconds"] == 60
    assert data["focus_count"] == 2
    assert data["watchlist_counts"]["actionable"] == 1
    assert data["watchlist_counts"]["priority"] == 3
    assert data["recent_ticker_count"] == 4
    assert data["backlog"] == {
        "recent_tickers": 4,
        "focus_results": 2,
        "watchlists_total": 4,
    }

def test_news_recent_endpoint():
    # Mock r.zrevrange and r.hget
    mock_redis_instance.zrevrange.return_value = ["article_id_1", "article_id_2"]
    
    mock_article_1 = {"title": "News 1", "url": "http://news.com/1"}
    mock_article_2 = {"title": "News 2", "url": "http://news.com/2"}
    
    # hget needs to be configured to return different values on subsequent calls
    mock_redis_instance.hget.side_effect = [
        json.dumps(mock_article_1),
        json.dumps(mock_article_2),
    ]

    response = client.get("/news/recent?limit=2")
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]['title'] == "News 1"
    assert data[1]['title'] == "News 2"
    mock_redis_instance.zrevrange.assert_called_with("news:recent:articles", 0, 1) # limit-1
    assert mock_redis_instance.hget.call_count == 2
    
def test_news_recent_empty():
    mock_redis_instance.zrevrange.return_value = []
    
    response = client.get("/news/recent")
    
    assert response.status_code == 200
    assert response.json() == []


def test_news_recent_replaces_legacy_tickers_with_llm_validated_entities():
    mock_redis_instance.zrevrange.return_value = ["article_id_1"]
    mock_redis_instance.hget.return_value = json.dumps({
        "title": "Company&#039;s US oil policy changes",
        "tickers": ["US"],
    })
    mock_redis_instance.hmget.return_value = [json.dumps({
        "display_headline": "Company&#039;s US oil policy changes",
        "relevance_label": "markets",
        "relevance_confidence": 0.95,
        "related_tickers": [],
        "related_commodities": ["Crude Oil"],
    })]

    response = client.get("/news/recent?limit=1")

    assert response.status_code == 200
    article = response.json()[0]
    assert article["tickers"] == []
    assert article["original_tickers"] == ["US"]
    assert article["ticker_source"] == "llm_company_validated"
    assert article["related_commodities"] == ["Crude Oil"]
    assert article["title"] == "Company's US oil policy changes"


def test_news_recent_supports_freshness_window_and_newest_sort():
    mock_redis_instance.zrevrangebyscore.return_value = ["fresh-article"]
    mock_redis_instance.hget.return_value = json.dumps({"title": "Fresh news"})

    with patch("routers.scanners_api.time.time", return_value=1_800_000_000):
        response = client.get("/news/recent?limit=20&max_age_hours=24&sort=newest")

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Fresh news"
    mock_redis_instance.zrevrangebyscore.assert_called_once_with(
        "news:recent:articles", "+inf", 1_799_913_600, start=0, num=20
    )


def test_news_recent_supports_offset_pagination():
    mock_redis_instance.zrevrangebyscore.return_value = []

    response = client.get("/news/recent?limit=100&offset=200&max_age_hours=336&sort=newest")

    assert response.status_code == 200
    mock_redis_instance.zrevrangebyscore.assert_called_once()
    assert mock_redis_instance.zrevrangebyscore.call_args.kwargs == {"start": 200, "num": 100}

def test_news_events_extracts_calendar_fields():
    mock_redis_instance.zrevrange.return_value = ["article_id_1"]
    mock_redis_instance.hmget.return_value = [json.dumps({
        "relevance_label": "finance",
        "relevance_confidence": 0.94,
        "display_headline": "AAPL reports earnings July 24",
        "llm_summary": "Analysts expect revenue growth.",
        "llm_topics": ["earnings"],
        "related_tickers": ["AAPL"],
        "related_companies": [{"name": "Apple Inc.", "validated_tickers": ["AAPL"]}],
    })]
    mock_redis_instance.hget.return_value = json.dumps({
        "title": "AAPL reports earnings on July 24",
        "summary": "Analysts expect revenue growth.",
        "source": "Earnings Wire",
        "url": "https://example.com/aapl-earnings",
        "published_ts": 1717200000,
        "tickers": ["AAPL"],
    })

    with patch("routers.scanners_api.NEWS_EVENT_LLM_ENABLED", False):
        response = client.get("/news/events?limit=1")

    assert response.status_code == 200
    data = response.json()
    event = next(item for item in data if item["id"] == "https://example.com/aapl-earnings")
    assert event["type"] == "Market note"
    assert event["date"] == "2024-05-31"
    assert event["details"]["inferred_event_date"] == "2024-07-24"
    assert event["details"]["news_topic"] == "earnings"
    assert event["symbols"] == ["AAPL"]
    assert event["details"]["relevance_label"] == "finance"
    assert event["details"]["llm_topics"] == ["earnings"]
    assert event["extraction_source"] == "news_rules"


def test_news_events_withholds_articles_pending_ai_classification():
    mock_redis_instance.zrevrange.return_value = ["article_id_1"]
    mock_redis_instance.hget.return_value = json.dumps({
        "title": "Possible market story",
        "source": "GDELT",
        "url": "https://example.com/pending",
        "published_ts": 1717200000,
        "tickers": ["MAYBE"],
    })

    response = client.get("/news/events?limit=1&include_generated=false")

    assert response.status_code == 200
    assert response.json() == []


def test_news_events_does_not_infer_ipo_dates_from_news():
    mock_redis_instance.zrevrange.return_value = ["article_id_1"]
    mock_redis_instance.hget.return_value = json.dumps({
        "title": "Example may launch an IPO next week",
        "source": "GDELT",
        "url": "https://example.com/ipo-rumor",
        "published_ts": 1717200000,
    })
    mock_redis_instance.hmget.return_value = [json.dumps({
        "relevance_label": "business",
        "relevance_confidence": 0.9,
        "display_headline": "Example may launch an IPO next week",
        "llm_topics": ["ipo"],
        "related_tickers": [],
    })]

    with patch("routers.scanners_api.NEWS_EVENT_LLM_ENABLED", False):
        response = client.get("/news/events?limit=1&include_generated=false")

    assert response.status_code == 200
    assert all(event["type"] != "IPO watch" for event in response.json())

def test_news_status_endpoint():
    def zcard_side_effect(key):
        return {
            "news:recent:articles": 12,
            "news:recent:tickers": 5,
        }.get(key, 0)

    mock_redis_instance.zcard.side_effect = zcard_side_effect
    mock_redis_instance.llen.side_effect = lambda key: {
        "gdelt:events:index:pending": 2,
        "gdelt:events:index:processing": 1,
        "gdelt:events:index:dead_letter": 4,
    }.get(key, 0)
    def get_side_effect(key):
        if key == "news:heartbeat":
            return json.dumps({"status": "ok", "ts": 1700000001})
        if key == "news:rss:verification_scores":
            return json.dumps({"marketwatch rss": {"coverage_score": 0.75}})
        if key == "newsapi:last_fetch_ts":
            return "1700000000"
        if key == "gdelt:last_fetch_ts":
            return "1700000010"
        if key == "gdelt:backfill:cursor_ts":
            return "1700000020"
        if key == "gdelt:export:backlog":
            return "1"
        if key == "gdelt:export:latest_url":
            return "http://data.gdeltproject.org/gdeltv2/20260716124500.export.CSV.zip"
        if key == "gdelt:export:lag_batches":
            return "3"
        if key == "news:dedup:original_count":
            return "12"
        if key == "news:dedup:consolidated_count":
            return "9"
        if key == "news:vector_dedup:original_count":
            return "20"
        if key == "news:vector_dedup:consolidated_count":
            return "16"
        if key == "news:graph_dedup:removed_count":
            return "5"
        if key.startswith("newsapi:daily_count:"):
            return "3"
        return None
    mock_redis_instance.get.side_effect = get_side_effect

    with (
        patch("routers.scanners_api.time.time", return_value=1700000061),
        patch("routers.scanners_api.GDELT_EVENT_COLLECTION_NAME", "gdelt_events_2"),
    ):
        response = client.get("/news/status")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["heartbeat"]["status"] == "ok"
    assert data["heartbeat_age_seconds"] == 60
    assert data["recent_article_count"] == 12
    assert data["recent_ticker_count"] == 5
    assert data["deduplication"]["duplicates_removed"] == 3
    assert data["deduplication"]["vector_duplicates_removed"] == 4
    assert data["deduplication"]["graph_nodes_removed"] == 5
    assert data["newsapi"]["daily_count"] == 3
    assert data["newsapi"]["last_fetch_ts"] == 1700000000
    assert data["newsapi"]["backfill_days"] == 30
    assert data["newsapi"]["backfill_max_pages"] >= 1
    assert data["rss"]["verification_scores"]["marketwatch rss"]["coverage_score"] == 0.75
    assert data["gdelt"]["enabled"] is True
    assert data["gdelt"]["last_fetch_ts"] == 1700000010
    assert data["gdelt"]["event_collection"] == "gdelt_events_2"
    assert data["gdelt"]["event_queue_pending"] == 2
    assert data["gdelt"]["event_queue_processing"] == 1
    assert data["gdelt"]["event_queue_dead_letter"] == 4
    assert data["gdelt"]["export_catchup_active"] is True
    assert data["gdelt"]["export_catchup_max_batches"] >= 1
    assert data["gdelt"]["export_lag_batches"] == 3
    assert data["gdelt"]["export_lag_minutes"] == 45
    assert data["gdelt"]["export_lag_alert"] is True
    assert data["gdelt"]["backfill_cursor_ts"] == 1700000020
    assert data["backlog"] == {
        "recent_articles": 12,
        "recent_tickers": 5,
        "gdelt_event_queue_pending": 2,
        "gdelt_event_queue_processing": 1,
        "gdelt_event_queue_dead_letter": 4,
    }


def test_decision_history_serializes_postgres_datetimes():
    created_at = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)
    with patch(
        "routers.scanners_api.list_decisions",
        return_value=[{"symbol": "HDB", "created_at": created_at}],
    ):
        response = client.get("/scanner/decisions/history?symbol=HDB&limit=5")

    assert response.status_code == 200
    assert response.json() == [
        {"symbol": "HDB", "created_at": "2026-09-14T12:30:00+00:00"}
    ]
