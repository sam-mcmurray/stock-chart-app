import pytest
from fastapi.testclient import TestClient
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from main import app
from routers import rag_api


def test_rag_query_smoke(monkeypatch):
    # Avoid running long-lived workers during tests
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    rag_api._RAG_CACHE.clear()

    monkeypatch.setattr(
        rag_api,
        "_query_vectors",
        lambda question, tickers, top_k: [
            {"title": "t1", "url": "u1", "tickers": ["AAPL"], "score": 0.1}
        ],
    )
    monkeypatch.setattr(
        rag_api,
        "_query_graph",
        lambda tickers: [
            {"ticker": "AAPL", "article_title": "Apple beats", "article_url": "u2", "entities": ["Apple"]}
        ],
    )
    monkeypatch.setattr(rag_api, "_embed_text", lambda text: [0.0] * 16)

    client = TestClient(app)
    res = client.post("/rag/", json={"question": "What about AAPL?", "tickers": ["AAPL"], "top_k": 2})

    assert res.status_code == 200
    data = res.json()
    assert data["question"] == "What about AAPL?"
    assert data["hits"][0]["title"] == "t1"
    assert data["graph_relations"][0]["ticker"] == "AAPL"
    assert isinstance(data.get("answer"), str)
    assert data["signal"]["hit_count"] == 1
    assert len(data["signal"]["embedding"]) == 16


def test_rag_query_legacy_field(monkeypatch):
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    rag_api._RAG_CACHE.clear()

    monkeypatch.setattr(rag_api, "_query_vectors", lambda q, t, k: [])
    monkeypatch.setattr(rag_api, "_query_graph", lambda t: [])

    client = TestClient(app)
    res = client.post("/rag/", json={"query": "legacy payload"})
    assert res.status_code == 200
    assert res.json()["question"] == "legacy payload"


def test_rag_query_fallback_summary_when_llm_unset(monkeypatch):
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    rag_api._RAG_CACHE.clear()

    monkeypatch.setattr(rag_api, "RAG_LLM_PROVIDER", "openai-compatible")
    monkeypatch.setattr(rag_api, "LLM_API_BASE", None)
    monkeypatch.setattr(rag_api, "_summarize", lambda q, d, r: "fallback-summary")
    monkeypatch.setattr(rag_api, "_query_vectors", lambda q, t, k: [])
    monkeypatch.setattr(rag_api, "_query_graph", lambda t: [])

    client = TestClient(app)
    res = client.post("/rag/", json={"question": "q1"})
    assert res.status_code == 200
    assert res.json()["answer"] == "fallback-summary"
    assert "signal" in res.json()


def test_gdelt_event_search_endpoint(monkeypatch):
    monkeypatch.setattr(
        rag_api,
        "_search_gdelt_events",
        lambda query, limit, offset: (
            100,
            [{
                "title": "Company announces earnings",
                "summary": "Structured GDELT event",
                "url": "https://example.com/earnings",
                "source": "example.com",
                "published_ts": 1721347200,
                "event_code": "010",
                "title_inferred": True,
                "score": 0.1,
            }],
        ),
    )

    client = TestClient(app)
    response = client.get("/rag/events/search?q=earnings&limit=5")

    assert response.status_code == 200
    assert response.json()["collection_count"] == 100
    assert response.json()["hits"][0]["title_inferred"] is True
    assert response.json()["offset"] == 0
    assert response.json()["returned"] == 1


def test_gdelt_event_search_pagination(monkeypatch):
    calls = []

    def search(query, limit, offset):
        calls.append((query, limit, offset))
        return 100, [{"rank": offset + 1, "title": "Result"}]

    monkeypatch.setattr(rag_api, "_search_gdelt_events", search)
    client = TestClient(app)
    response = client.get("/rag/events/search?q=merger&limit=25&offset=25")

    assert response.status_code == 200
    assert calls == [("merger", 25, 25)]
    assert response.json()["offset"] == 25
    assert response.json()["has_more"] is False


def test_gdelt_event_search_merges_shards_by_distance(monkeypatch):
    class Store:
        def count(self, collection):
            return 2

        def query(self, collection, text, limit):
            return [
                {"score": 0.6, "payload": {"title": "older shard", "document": "older summary", "url": "https://example.com/older", "country_code": "US"}},
                {"score": 0.9, "payload": {"title": "new shard", "document": "new summary", "url": "https://example.com/new", "country_code": "US"}},
            ]

    monkeypatch.setattr(
        rag_api,
        "get_store",
        lambda: Store(),
    )
    monkeypatch.setattr(rag_api, "resolve_company_tickers", lambda text: ["MSFT", "NVDA"])

    count, hits = rag_api._search_gdelt_events("earnings", limit=2)

    assert count == 2
    assert [hit["title"] for hit in hits] == ["new shard", "older shard"]
    assert [hit["rank"] for hit in hits] == [1, 2]
    assert hits[0]["tickers"] == ["MSFT", "NVDA"]
    assert hits[0]["country_code"] == "US"


def test_gdelt_related_articles(monkeypatch):
    monkeypatch.setattr(
        rag_api.persistent_store,
        "list_gdelt_mentions",
        lambda event_id, limit: [{"event_id": event_id, "url": "https://example.com/story"}],
    )
    client = TestClient(app)
    response = client.get("/rag/events/1307007199/related?limit=5")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["articles"][0]["url"] == "https://example.com/story"


def test_gdelt_related_articles_rejects_invalid_id():
    client = TestClient(app)
    response = client.get("/rag/events/not-an-id/related")
    assert response.status_code == 400
