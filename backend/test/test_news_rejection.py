import os
import uuid
from unittest.mock import Mock

import pytest

from services import news_rejection as rejection, persistent_store as store


@pytest.fixture
def db(monkeypatch):
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "true":
        pytest.skip("Requires isolated PostgreSQL integration schema")
    from psycopg import connect, sql
    schema = "news_rejection_test_" + uuid.uuid4().hex
    with connect(store.DATABASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        def scoped(*args, **kwargs):
            return connect(store.DATABASE_URL, options=f"-c search_path={schema}", **kwargs)
        monkeypatch.setattr(store.psycopg, "connect", scoped)
        try:
            with scoped() as conn:
                conn.execute("""
                    CREATE TABLE worker_checkpoints(name TEXT PRIMARY KEY,value JSONB,updated_at TIMESTAMPTZ);
                    CREATE TABLE historical_article_enrichments(url TEXT PRIMARY KEY,result JSONB,
                        relevance_label TEXT, original_title TEXT,display_headline TEXT,related_tickers JSONB,
                        related_companies JSONB,company_relationships JSONB,supporting_url TEXT,content_source TEXT);
                    CREATE TABLE historical_article_graph_outbox(url TEXT PRIMARY KEY,result JSONB,next_attempt_at TIMESTAMPTZ DEFAULT NOW());
                    CREATE TABLE calendar_events(id TEXT PRIMARY KEY,url TEXT,details JSONB);
                    CREATE TABLE news_enrichment_versions(article_url TEXT,result JSONB);
                """)
            yield scoped
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def unrelated():
    return {"relevance_label": "unrelated", "relevance_confidence": .99,
            "relevance_policy_version": "finance-gate-v1", "display_headline": "Crime story",
            "llm_summary": "Unneeded article content"}


def test_bounded_cleanup_preserves_relevant_and_ambiguous_records(db):
    with db() as conn:
        for url, result in [("a", unrelated()), ("b", {"relevance_label": "business"}),
                            ("c", {**unrelated(), "relevance_confidence": .4})]:
            conn.execute("INSERT INTO historical_article_enrichments(url,result,relevance_label,original_title) VALUES(%s,%s,%s,'old body')",
                         (url, store.jsonb(result), result["relevance_label"]))
    assert rejection.queue_existing_rejections(limit=1) == 1
    assert rejection.queue_existing_rejections(limit=1) == 0
    assert rejection.queue_existing_rejections(limit=1) == 0
    with db() as conn:
        rows = dict(conn.execute("SELECT url,result FROM historical_article_enrichments").fetchall())
        assert rows["a"]["rejected_receipt"]
        assert "llm_summary" not in rows["a"]
        assert rows["b"] == {"relevance_label": "business"}
        assert "llm_summary" in rows["c"]
        assert conn.execute("SELECT count(*) FROM historical_article_graph_outbox").fetchone()[0] == 1


def test_known_rejections_block_reingestion_but_not_uncertain_or_business(db):
    with db() as conn:
        for url, result in [("https://test.invalid/crime", unrelated()),
                            ("https://test.invalid/business", {"relevance_label": "business"}),
                            ("https://test.invalid/uncertain", {**unrelated(), "relevance_confidence": .2})]:
            conn.execute("INSERT INTO historical_article_enrichments(url,result,relevance_label) VALUES(%s,%s,%s)",
                         (url, store.jsonb(result), result["relevance_label"]))
    items = [{"url": "https://www.test.invalid/crime?utm_source=feed"},
             {"url": "https://test.invalid/business"}, {"url": "https://test.invalid/uncertain"},
             {"url": "https://test.invalid/new"}]
    assert rejection.exclude_known_rejections(items) == items[1:]
    assert rejection.known_rejections(items)[items[0]["url"]]["rejected_receipt"]


def test_rejected_delivery_removes_active_content_and_retries_graph_failures(db, monkeypatch):
    from services import article_graph, news_cleanup_queue
    graph = Mock(return_value=-1)
    enqueue = Mock(return_value=1)
    monkeypatch.setattr(article_graph, "remove_superseded_article_nodes", graph)
    monkeypatch.setattr(news_cleanup_queue, "enqueue", enqueue)
    with db() as conn:
        conn.execute("INSERT INTO calendar_events VALUES ('news-id','a','{\"calendar_kind\":\"market_news\"}'),('ipo-id','a','{}')")
        conn.execute("INSERT INTO news_enrichment_versions VALUES ('a','{}'),('b','{}')")
    client = Mock()
    with pytest.raises(RuntimeError, match="graph deletion"):
        rejection.deliver("a", unrelated(), client)
    assert "news-id" in enqueue.call_args.args[0]
    assert rejection.article_dedup_id({"url": "a"}) in enqueue.call_args.args[0]
    assert set(graph.call_args.args[0]) == {"a", rejection.canonicalize_url("a")}
    with db() as conn:
        assert conn.execute("SELECT id FROM calendar_events").fetchall() == [("ipo-id",)]
        assert conn.execute("SELECT article_url FROM news_enrichment_versions").fetchall() == [("b",)]
    graph.return_value = 0
    assert rejection.deliver("a", unrelated(), client)
