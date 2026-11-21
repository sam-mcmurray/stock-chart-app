import os
from contextlib import contextmanager

import pytest
from services import persistent_store as store
from services import company_relationship_graph


@pytest.fixture
def database(monkeypatch):
    if os.getenv("DRL_SQL_INTEGRATION") != "1":
        pytest.skip("Isolated PostgreSQL transaction tests")
    with store.psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        for name in ("historical_article_enrichments", "news_enrichment_versions", "historical_article_retries", "worker_checkpoints"):
            conn.execute(f"CREATE TEMP TABLE {name} (LIKE public.{name} INCLUDING ALL)")
        conn.execute("""CREATE TEMP TABLE historical_article_graph_outbox (
            url text PRIMARY KEY, result jsonb NOT NULL, attempts int NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
        conn.execute("SET search_path TO pg_temp")

        @contextmanager
        def connect(*args, **kwargs):
            previous = conn.row_factory
            conn.row_factory = kwargs.get("row_factory", previous)
            try:
                with conn.transaction():
                    yield conn
            finally:
                conn.row_factory = previous

        monkeypatch.setattr(store.psycopg, "connect", connect)
        monkeypatch.setattr(store, "ensure_schema", lambda: None)
        monkeypatch.setattr(store, "enabled", lambda: True)
        yield conn


def rows():
    return [({"url": "one", "published_ts": 123}, {"relevance_label": "business", "relevance_source": "llm"}),
            ({"url": "two", "published_ts": 124}, {"relevance_label": "markets", "relevance_source": "llm"})]


def test_rejected_article_saves_only_receipt_and_durable_deletion(database):
    result = {"relevance_label": "unrelated", "relevance_confidence": .99,
              "relevance_policy_version": "finance-gate-v1", "relevance_source": "llm",
              "display_headline": "Crime story", "llm_summary": "Unwanted full content"}
    store.save_historical_article_results([({"url": "crime", "title": "Original crime story",
                                            "article_text": "Full original body"}, result)], "v1")
    saved = database.execute("SELECT original_title,display_headline,result FROM historical_article_enrichments WHERE url='crime'").fetchone()
    assert saved[0] == saved[1] == ""
    assert saved[2]["rejected_receipt"]
    assert "llm_summary" not in saved[2]
    outbox = database.execute("SELECT result FROM historical_article_graph_outbox WHERE url='crime'").fetchone()[0]
    assert outbox["rejected_receipt"]
    assert "display_headline" not in outbox


def test_related_checkpoints_commit_and_load_together(database):
    store.save_worker_checkpoints({"history": {"completed": 8}, "backfill": {"completed": 8}})
    assert store.load_worker_checkpoint("history") == {"completed": 8}
    assert store.load_worker_checkpoint("backfill") == {"completed": 8}
    store.save_worker_checkpoint("history", {"completed": 16})
    assert store.load_worker_checkpoint("history") == {"completed": 16}


def test_failed_checkpoint_group_does_not_partially_advance(database):
    store.save_worker_checkpoints({"history": {"completed": 8}, "backfill": {"completed": 8}})
    with pytest.raises(store.psycopg.errors.NotNullViolation):
        store.save_worker_checkpoints({"history": {"completed": 16}, None: {"completed": 16}})
    assert store.load_worker_checkpoint("history") == {"completed": 8}
    assert store.load_worker_checkpoint("backfill") == {"completed": 8}


def test_batch_commit_and_delivery_failure_keep_durable_results(database):
    store.save_historical_article_results(rows(), "v1")
    assert database.execute("SELECT count(*) FROM historical_article_enrichments").fetchone()[0] == 2
    assert database.execute("SELECT count(*) FROM news_enrichment_versions").fetchone()[0] == 2
    assert store.historical_graph_backlog_full(2)
    delivered = []

    def fail(url, result):
        assert result["published_ts"] in (123, 124)
        raise RuntimeError("Neo4j unavailable")

    assert store.deliver_historical_graph_result(fail) == "retry"
    assert database.execute("SELECT sum(attempts) FROM historical_article_graph_outbox").fetchone()[0] == 1
    database.execute("UPDATE historical_article_graph_outbox SET next_attempt_at = now()")
    for _ in range(2):
        assert store.deliver_historical_graph_result(lambda url, result: delivered.append(url) or True) == "delivered"
    assert sorted(delivered) == ["one", "two"]
    assert store.deliver_historical_graph_result(fail) == "idle"
    assert database.execute("SELECT count(*) FROM historical_article_enrichments").fetchone()[0] == 2


def test_invalid_second_result_rolls_back_whole_batch(database):
    batch = rows()
    batch[1][1]["processing_error"] = "bad result"
    with pytest.raises(ValueError, match="failed inference"):
        store.save_historical_article_results(batch, "v1")
    for table in ("historical_article_enrichments", "news_enrichment_versions", "historical_article_graph_outbox"):
        assert database.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_crash_after_graph_write_replays_without_new_model_result(database):
    store.save_historical_article_results(rows()[:1], "v1")
    graph = set()

    def crash(url, result):
        graph.add(url)
        raise SystemExit("crash before acknowledgement")

    with pytest.raises(SystemExit):
        store.deliver_historical_graph_result(crash)
    assert database.execute("SELECT count(*) FROM historical_article_graph_outbox").fetchone()[0] == 1
    assert store.deliver_historical_graph_result(lambda url, result: graph.add(url) or True) == "delivered"
    assert graph == {"one"}
    assert database.execute("SELECT count(*) FROM news_enrichment_versions").fetchone()[0] == 1


def test_recovery_requeues_saved_results_and_clears_old_retry(database):
    store.save_historical_article_results(rows()[:1], "v1")
    database.execute("DELETE FROM historical_article_graph_outbox")
    store.enqueue_historical_article_retry({"url": "one"}, "v1", "old graph failure")
    store.recover_historical_article_results(["one"])
    assert database.execute("SELECT count(*) FROM historical_article_retries").fetchone()[0] == 0
    assert database.execute("SELECT result->>'published_ts' FROM historical_article_graph_outbox").fetchone()[0] == "123"
    store.recover_historical_article_results(["one"])
    assert database.execute("SELECT count(*) FROM historical_article_graph_outbox").fetchone()[0] == 1


def test_historical_retry_backoff_grows_and_caps_without_dropping_record(database):
    article = {"url": "retry", "published_ts": 123}
    for expected_attempts, expected_seconds in [(1, 300), (2, 600), (3, 1200)]:
        store.enqueue_historical_article_retry(article, "v1", "translation failed")
        attempts, seconds = database.execute(
            "SELECT attempts, extract(epoch FROM next_attempt_at - updated_at) "
            "FROM historical_article_retries WHERE url = 'retry'"
        ).fetchone()
        assert attempts == expected_attempts
        assert float(seconds) == pytest.approx(expected_seconds, abs=1)
        assert store.list_historical_article_retries() == []
    database.execute("UPDATE historical_article_retries SET attempts = 1064")
    store.enqueue_historical_article_retry(article, "v1", "still unresolved")
    attempts, seconds = database.execute(
        "SELECT attempts, extract(epoch FROM next_attempt_at - updated_at) "
        "FROM historical_article_retries WHERE url = 'retry'"
    ).fetchone()
    assert attempts == 1065
    assert float(seconds) == pytest.approx(86400, abs=1)
    database.execute("UPDATE historical_article_retries SET next_attempt_at = now() - interval '1 second'")
    assert store.list_historical_article_retries()[0]["url"] == "retry"
    store.save_historical_article_results([(article, {"relevance_source": "llm", "relevance_label": "business"})], "v1")
    assert database.execute("SELECT count(*) FROM historical_article_retries").fetchone()[0] == 0


def test_strict_relationship_delivery_propagates_errors(monkeypatch):
    monkeypatch.setattr(company_relationship_graph, "_driver", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        company_relationship_graph._upsert_rows([{"ticker_a": "A"}], strict=True)
    assert company_relationship_graph._upsert_rows([{"ticker_a": "A"}]) == 0


def test_stale_graph_job_does_not_replace_newer_article_edges(monkeypatch):
    from unittest.mock import MagicMock
    from services import article_graph
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.execute_query.side_effect = [([], None, None), ([{"url": "one"}], None, None)]
    monkeypatch.setattr(article_graph, "_driver", lambda: driver)
    monkeypatch.setattr(article_graph, "replace_article_security_edges", lambda *a: pytest.fail("stale edges"))
    assert article_graph.update_article_ai_enrichment("one", {"relevance_classified_at": 10}, strict=True)
    assert driver.execute_query.call_args_list[0].kwargs["strict"] is True
