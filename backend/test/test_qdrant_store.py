from services import qdrant_store


def test_point_id_is_deterministic_uuid():
    first = qdrant_store.point_id("article-1")
    assert first == qdrant_store.point_id("article-1")
    assert first != qdrant_store.point_id("article-2")
    assert len(first) == 36


def test_upsert_items_preserves_original_id_and_payload(monkeypatch):
    store = qdrant_store.QdrantStore("http://qdrant")
    captured = []
    monkeypatch.setattr(qdrant_store, "embed_texts", lambda texts: [[0.1, 0.2] for _ in texts])
    monkeypatch.setattr(
        store,
        "upsert_points",
        lambda collection, points, **kwargs: captured.extend(points),
    )

    count = store.upsert_items("news", [{
        "id": "source-id",
        "url": "https://example.com/story",
        "title": "Story",
        "summary": "Article text",
        "published_ts": 123,
        "tickers": ["AAPL"],
    }])

    assert count == 1
    assert captured[0]["id"] == qdrant_store.point_id("source-id")
    assert captured[0]["payload"]["original_id"] == "source-id"
    assert captured[0]["payload"]["document"] == "Article text"
    assert captured[0]["payload"]["tickers"] == ["AAPL"]


def test_query_extracts_points(monkeypatch):
    store = qdrant_store.QdrantStore("http://qdrant")
    monkeypatch.setattr(qdrant_store, "embed_texts", lambda texts: [[0.1, 0.2]])
    monkeypatch.setattr(
        store,
        "_request",
        lambda *args, **kwargs: {"result": {"points": [{"id": "p1", "score": 0.9, "payload": {"title": "Hit"}}]}},
    )

    assert store.query("news", "query", 5)[0]["payload"]["title"] == "Hit"
