# backend/test/test_news_worker.py
import pytest
from unittest.mock import patch, MagicMock
import json
import os

# Adjust path for imports if running pytest from project root
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from workers import news_worker as news_worker_module
from workers.news_worker import NewsWorker, _now_ts, simple_extract_tickers
from services.news_indexer import NewsIndexer # Assuming NewsIndexer is simple enough or mocked

@pytest.fixture
def mock_redis():
    mock_redis_instance = MagicMock()
    mock_redis_instance.pipeline.return_value = MagicMock() # Mock pipeline
    with patch.object(news_worker_module, "r", mock_redis_instance):
        yield mock_redis_instance

@pytest.fixture
def mock_news_sources():
    with patch('workers.news_worker.load_finnhub_general_news') as mock_finnhub, \
         patch('workers.news_worker.load_newsapi_backfill_articles') as mock_newsapi:
        yield mock_finnhub, mock_newsapi

@pytest.fixture
def news_worker(mock_redis):
    # Mock NewsIndexer as it's not the focus of this test
    mock_indexer = MagicMock(spec=NewsIndexer)
    mock_indexer.col = MagicMock()
    return NewsWorker(indexer=mock_indexer)

def test_fetch_news_integration(mock_news_sources):
    mock_finnhub, mock_newsapi = mock_news_sources
    
    finnhub_data = [{
        "url": "http://finnhub.com/news1",
        "headline": "Finnhub News Title",
        "summary": "Finnhub News Summary",
        "datetime": _now_ts(), # Finnhub uses epoch directly
        "source": "Finnhub",
        "id": 123, "related": [], "image": ""
    }]
    newsapi_data = [{
        "url": "http://newsapi.com/article1",
        "title": "NewsAPI Headline",
        "description": "NewsAPI Description",
        "published_ts": _now_ts(),
        "source": {"name": "NewsAPI Source"},
        "id": hash("http://newsapi.com/article1"), "related": [], "image": ""
    }]

    mock_finnhub.return_value = finnhub_data
    mock_newsapi.return_value = newsapi_data

    with (
        patch("workers.news_worker.GDELT_ENABLED", False),
        patch("workers.news_worker.NEWSAPI_ENABLED", True),
        patch("workers.news_worker._newsapi_allowed", return_value=True),
            patch("workers.news_worker._newsapi_remaining_budget", return_value=1),
            patch("workers.news_worker._fetch_finnhub_current", return_value=[news_worker_module._normalize_article(finnhub_data[0], "Finnhub")]),
            patch("workers.news_worker._fetch_gdelt_export_cycle", return_value=([], "", None)),
            patch("workers.news_worker._fetch_rss_current", return_value=[]),
            patch("workers.news_worker._article_exists", return_value=False),
    ):
        items = news_worker_module._fetch_news()
        
        assert len(items) == 2
        assert any(item["title"] == "Finnhub News Title" for item in items)
        assert any(item["title"] == "NewsAPI Headline" for item in items)

def test_ticker_extraction_preserves_case_and_filters_news_terms():
    text = "Some investors watch AAPL and $MSFT after the CEO discusses an IPO in the USA and UK."

    assert simple_extract_tickers(text) == ["AAPL", "MSFT"]


def test_article_normalization_does_not_extract_summary_event_labels_as_tickers():
    article = {
        "url": "https://example.com/market-update",
        "headline": "general market update",
        "summary": "GDELT event involving ITALY and AUSTRALIA",
        "datetime": _now_ts(),
        "source": "example.com",
    }

    normalized = news_worker_module._normalize_article(article, "GDELT Export")

    assert normalized["tickers"] == []
    assert normalized["provider"] == "gdelt export"


def test_article_normalization_accepts_publisher_derived_tickers():
    article = {
        "url": "https://example.com/stocks",
        "headline": "Three stocks to buy",
        "summary": "Publisher summary",
        "tickers": ["AMZN", "ACME"],
        "datetime": _now_ts(),
    }

    normalized = news_worker_module._normalize_article(article, "GDELT Export")

    assert normalized["tickers"] == ["ACME", "AMZN"]


def test_article_normalization_preserves_canonical_gdelt_identity_and_metadata():
    article = {
        "id": "gdelt:20260716:abc123",
        "url": "https://example.com/market-event",
        "headline": "Market event",
        "summary": "Event summary",
        "datetime": _now_ts(),
        "event_id": "1307007199",
        "metadata": {"gdelt_event_id": "1307007199", "event_code": "010"},
    }

    normalized = news_worker_module._normalize_article(article, "GDELT Export")

    assert normalized["id"] == article["id"]
    assert normalized["metadata"]["gdelt_event_id"] == "1307007199"
    assert normalized["metadata"]["event_code"] == "010"


def test_news_worker_routes_live_gdelt_events_to_canonical_index(news_worker):
    gdelt_indexer = MagicMock(spec=NewsIndexer)
    news_worker.gdelt_indexer = gdelt_indexer
    gdelt_event = {
        "id": "gdelt:20260716:abc123",
        "event_id": "1307007199",
        "provider": "gdelt export",
        "url": "https://example.com/event",
        "title": "Event",
    }
    supplement = {
        "provider": "finnhub",
        "url": "https://example.com/supplement",
        "title": "Supplement",
    }

    news_worker._index_items([gdelt_event, supplement])

    indexed = news_worker.indexer.upsert_batch.call_args.args[0]
    assert len(indexed) == 1
    assert indexed[0]["canonical_url"] == "https://example.com/supplement"
    assert indexed[0]["id"] == indexed[0]["dedup_id"]
    assert indexed[0]["metadata"]["providers"] == "finnhub"
    gdelt_indexer.upsert_batch.assert_called_once_with([gdelt_event])


def test_news_worker_queues_live_gdelt_events_when_no_direct_indexer(news_worker):
    gdelt_event = {
        "id": "gdelt:20260716:abc123",
        "event_id": "1307007199",
        "provider": "gdelt export",
        "url": "https://example.com/event",
        "title": "Event",
    }

    with patch.object(news_worker_module, "enqueue_gdelt_events") as enqueue:
        news_worker._index_items([gdelt_event])

    enqueue.assert_called_once_with([gdelt_event], client=news_worker_module.r)


def test_rss_fetch_keeps_known_articles_for_provider_merge(mock_redis):
    articles = [
        {"url": "https://example.com/known", "headline": "Known article"},
        {"url": "https://example.com/new", "headline": "New article"},
    ]

    with (
        patch.object(news_worker_module, "_interval_allowed", return_value=True),
        patch.object(news_worker_module, "_mark_timestamp"),
        patch.object(
            news_worker_module,
            "_article_exists",
            side_effect=lambda url: url.endswith("/known"),
        ),
        patch.object(
            news_worker_module,
            "_article_has_other_provider",
            side_effect=lambda item, _provider: item["url"].endswith("/known"),
        ),
    ):
        items = news_worker_module._fetch_rss_current(
            lambda: articles,
            "rss:last-fetch",
            True,
            "Example RSS",
        )

    assert len(items) == 2
    assert items[0]["_already_known"] is True
    assert items[1]["_already_known"] is False
    assert items[0]["_verification_match"] is True
    assert items[1]["_verification_match"] is False


def test_rss_verification_score_counts_only_other_provider_matches(mock_redis):
    mock_redis.get.return_value = None
    items = [
        {"provider": "marketwatch rss", "_verification_match": True},
        {"provider": "marketwatch rss", "_verification_match": False},
        {"provider": "thestreet rss", "_verification_match": True},
        {"provider": "yahoo rss", "_verification_match": True},
    ]

    news_worker_module._record_rss_verification(items)

    payload = json.loads(mock_redis.set.call_args.args[1])
    assert payload["marketwatch rss"]["coverage_score"] == 0.5
    assert payload["marketwatch rss"]["matched_by_other_provider"] == 1
    assert payload["thestreet rss"]["coverage_score"] == 1.0
    assert "yahoo rss" not in payload


def test_news_worker_does_not_reindex_known_rss_article(news_worker):
    known_rss = {
        "provider": "yahoo rss",
        "url": "https://example.com/known",
        "title": "Known article",
        "_already_known": True,
    }
    new_rss = {
        "provider": "yahoo rss",
        "url": "https://example.com/new",
        "title": "New article",
        "_already_known": False,
    }

    news_worker._index_items([known_rss, new_rss])

    indexed = news_worker.indexer.upsert_batch.call_args.args[0]
    assert len(indexed) == 1
    assert indexed[0]["url"] == "https://example.com/new"


def test_gdelt_export_cycle_processes_missing_batches_in_order(mock_redis):
    previous = "http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"
    first = "http://data.gdeltproject.org/gdeltv2/20260716121500.export.CSV.zip"
    latest = "http://data.gdeltproject.org/gdeltv2/20260716123000.export.CSV.zip"
    mock_redis.get.side_effect = lambda key: previous if key == news_worker_module.K_GDELT_EXPORT_LAST_URL else None
    batches = {
        first: [{"id": "gdelt:first", "event_id": "1", "url": "https://example.com/first", "headline": "First"}],
        latest: [{"id": "gdelt:latest", "event_id": "2", "url": "https://example.com/latest", "headline": "Latest"}],
    }

    with (
        patch.object(news_worker_module, "get_latest_gdelt_export_url", return_value=latest),
        patch.object(news_worker_module, "load_gdelt_export_url", side_effect=lambda url: batches[url]) as load,
        patch.object(news_worker_module, "load_gdelt_export_mentions", return_value=[]),
        patch.object(news_worker_module, "GDELT_ARTICLE_METADATA_ENABLED", False),
        patch.object(news_worker_module.persistent_store, "upsert_gdelt_mentions"),
    ):
        items, completed_url, caught_up = news_worker_module._fetch_gdelt_export_cycle()

    assert [call.args[0] for call in load.call_args_list] == [first, latest]
    assert [item["id"] for item in items] == ["gdelt:first", "gdelt:latest"]
    assert completed_url == latest
    assert caught_up is True


def test_gdelt_export_cursor_is_not_changed_when_poll_is_gated(mock_redis):
    news_worker_module._commit_gdelt_export_cursor("", None)

    mock_redis.pipeline.assert_not_called()


def test_article_normalization_does_not_treat_geography_as_tickers():
    article = {
        "url": "https://example.com/west-bank-update",
        "headline": "WEST BANK tensions rise after talks",
        "summary": "A geopolitical update unrelated to listed companies.",
        "datetime": _now_ts(),
    }

    normalized = news_worker_module._normalize_article(article, "GDELT")

    assert normalized["tickers"] == []

def test_news_indexer_omits_empty_ticker_metadata():
    indexer = NewsIndexer.__new__(NewsIndexer)
    indexer.col = MagicMock()

    indexer.upsert_batch([
        {"url": "https://example.com/general", "title": "General market update", "tickers": []},
        {"url": "https://example.com/aapl", "title": "AAPL update", "tickers": ["AAPL"]},
    ])

    metadatas = indexer.col.upsert.call_args.kwargs["metadatas"]
    assert "tickers" not in metadatas[0]
    assert metadatas[1]["tickers"] == ["AAPL"]

def test_news_indexer_uses_explicit_id_and_scalar_metadata():
    indexer = NewsIndexer.__new__(NewsIndexer)
    indexer.col = MagicMock()

    indexer.upsert_batch([{
        "id": "gdelt:123",
        "url": "https://example.com/event",
        "title": "Market event",
        "metadata": {"event_code": "010", "tone": -2.5, "ignored": ["not", "scalar"]},
    }])

    call = indexer.col.upsert.call_args.kwargs
    assert call["ids"] == ["gdelt:123"]
    assert call["metadatas"][0]["event_code"] == "010"
    assert call["metadatas"][0]["tone"] == -2.5
    assert "ignored" not in call["metadatas"][0]


def test_news_indexer_can_preserve_existing_canonical_events():
    indexer = NewsIndexer.__new__(NewsIndexer)
    indexer.col = MagicMock()
    indexer.col.get.return_value = {"ids": ["gdelt:existing"]}

    indexer.upsert_batch([
        {"id": "gdelt:existing", "url": "https://example.com/existing", "title": "Existing"},
        {"id": "gdelt:new", "url": "https://example.com/new", "title": "New"},
    ], preserve_existing=True)

    call = indexer.col.upsert.call_args.kwargs
    assert call["ids"] == ["gdelt:new"]

def test_add_recent(news_worker, mock_redis):
    items = [
        {
            "id": "gdelt:20260716:abc123",
            "url": "http://test.com/article1",
            "title": "Test Title 1",
            "summary": "Test Summary 1",
            "published_ts": _now_ts() - 100,
            "source": "TestSource",
            "provider": "gdelt",
            "tickers": ["AAPL"],
            "metadata": {"gdelt_event_id": "1307007199"},
        },
        {
            "url": "http://test.com/article2",
            "title": "Test Title 2",
            "summary": "Test Summary 2",
            "published_ts": _now_ts() - 50,
            "source": "TestSource",
            "tickers": ["GOOG"]
        }
    ]
    news_worker._add_recent(items)

    pipe = mock_redis.pipeline.return_value
    assert pipe.hset.call_count == 2
    assert pipe.zadd.call_count == 4 # 2 for articles, 2 for tickers
    pipe.execute.assert_called_once()
    stored = pipe.hset.call_args_list[0].kwargs["mapping"]["json"]
    assert json.loads(stored)["summary"] == "Test Summary 1"
    assert json.loads(stored)["provider"] == "gdelt"

    # Check a sample hset call
    first_hset_call = pipe.hset.call_args_list[0]
    _, hset_kwargs = first_hset_call
    assert hset_kwargs['mapping']['json'] is not None
    json_data = json.loads(hset_kwargs['mapping']['json'])
    assert json_data['title'] == "Test Title 1"
    assert "AAPL" in json_data['tickers']


def test_add_recent_consolidates_url_variants_and_preserves_providers(news_worker, mock_redis):
    mock_redis.hget.return_value = None
    items = [
        {
            "url": "http://www.example.com/story?utm_source=gdelt",
            "title": "Company reports record quarterly earnings after market close",
            "summary": "Short summary.",
            "provider": "gdelt export",
            "published_ts": _now_ts() - 10,
        },
        {
            "url": "https://example.com/story",
            "title": "Company reports record quarterly earnings after market close",
            "summary": "A longer summary with substantially more useful financial detail.",
            "provider": "finnhub",
            "published_ts": _now_ts(),
        },
    ]

    news_worker._add_recent(items)

    pipe = mock_redis.pipeline.return_value
    article_calls = [call for call in pipe.hset.call_args_list if str(call.args[0]).startswith("news:article:")]
    assert len(article_calls) == 1
    stored = json.loads(article_calls[0].kwargs["mapping"]["json"])
    assert stored["providers"] == ["finnhub", "gdelt export"]
    assert stored["canonical_url"] == "https://example.com/story"
    assert len(stored["source_urls"]) == 2


def test_migrate_vector_dedup_upserts_before_deleting_superseded_ids(news_worker, mock_redis):
    mock_redis.get.return_value = None
    news_worker.indexer.col.count.return_value = 2
    news_worker.indexer.col.get.return_value = {
        "ids": ["old-one", "old-two"],
        "documents": ["Short summary", "Richer financial summary"],
        "metadatas": [
            {
                "url": "http://www.example.com/story?utm_source=gdelt",
                "title": "Company reports record quarterly earnings after market close",
                "provider": "gdelt",
                "published_ts": 100,
            },
            {
                "url": "https://example.com/story",
                "title": "Company reports record quarterly earnings after market close",
                "provider": "finnhub",
                "published_ts": 110,
            },
        ],
    }

    news_worker._migrate_vector_dedup()

    consolidated = news_worker.indexer.upsert_batch.call_args.args[0]
    assert len(consolidated) == 1
    assert consolidated[0]["metadata"]["providers"] == "finnhub,gdelt"
    news_worker.indexer.col.delete.assert_called_once_with(ids=["old-one", "old-two"])
    mock_redis.set.assert_any_call(
        news_worker_module.K_NEWS_VECTOR_DEDUP_VERSION,
        news_worker_module.NEWS_VECTOR_DEDUP_VERSION,
    )


def test_migrate_graph_dedup_removes_only_noncanonical_source_urls(news_worker, mock_redis):
    mock_redis.get.return_value = None
    mock_redis.zrangebyscore.return_value = ["article-one"]
    mock_redis.hget.return_value = json.dumps({
        "url": "https://example.com/story",
        "canonical_url": "https://example.com/story",
        "source_urls": [
            "http://www.example.com/story?utm_source=gdelt",
            "https://example.com/story",
        ],
    })

    with (
        patch.object(news_worker_module, "upsert_article_graph") as upsert,
        patch.object(news_worker_module, "remove_superseded_article_nodes", return_value=1) as remove,
    ):
        news_worker._migrate_graph_dedup()

    upsert.assert_called_once()
    remove.assert_called_once_with(["http://www.example.com/story?utm_source=gdelt"])
    mock_redis.set.assert_any_call(news_worker_module.K_NEWS_GRAPH_DEDUP_REMOVED_COUNT, 1)
    mock_redis.set.assert_any_call(
        news_worker_module.K_NEWS_GRAPH_DEDUP_VERSION,
        news_worker_module.NEWS_GRAPH_DEDUP_VERSION,
    )

def test_migrate_recent_tickers_rebuilds_legacy_candidates(news_worker, mock_redis):
    mock_redis.get.return_value = None
    mock_redis.zrangebyscore.return_value = [("article-1", 123.0)]
    mock_redis.hget.return_value = json.dumps({
        "title": "Some investors watch AAPL while the CEO discusses an IPO",
        "tickers": ["SOME", "AAPL", "CEO", "IPO"],
    })

    news_worker._migrate_recent_tickers()

    pipe = mock_redis.pipeline.return_value
    pipe.delete.assert_called_once_with(news_worker_module.Z_TICKERS)
    pipe.zadd.assert_not_called()
    migrated = json.loads(pipe.hset.call_args.kwargs["mapping"]["json"])
    assert migrated["tickers"] == []
    pipe.set.assert_called_once_with(
        news_worker_module.K_TICKER_EXTRACTION_VERSION,
        news_worker_module.TICKER_EXTRACTION_VERSION,
    )
    pipe.execute.assert_called_once()


def test_migrate_recent_tickers_repairs_future_article_scores(news_worker, mock_redis):
    now_ts = 1_000
    mock_redis.get.return_value = None
    mock_redis.zrangebyscore.return_value = [("article-1", 10_000.0)]
    mock_redis.hget.return_value = json.dumps({
        "title": "Market update",
        "published_ts": 10_000,
        "tickers": [],
    })

    with patch("workers.news_worker._now_ts", return_value=now_ts):
        news_worker._migrate_recent_tickers()

    pipe = mock_redis.pipeline.return_value
    assert any(
        call.args == (news_worker_module.Z_ARTICLES, {"article-1": now_ts})
        for call in pipe.zadd.call_args_list
    )
    repaired = json.loads(pipe.hset.call_args.kwargs["mapping"]["json"])
    assert repaired["published_ts"] == now_ts

def test_prune_old(news_worker, mock_redis):
    # Mock some old and new articles in Redis
    mock_redis.zrangebyscore.return_value = ["old_article_id_1", "old_article_id_2"]
    
    news_worker._prune_old()
    
    pipe = mock_redis.pipeline.return_value
    assert pipe.delete.call_count == 2
    mock_redis.zremrangebyscore.call_count == 2 # Once for articles, once for tickers
    pipe.execute.assert_called_once()
