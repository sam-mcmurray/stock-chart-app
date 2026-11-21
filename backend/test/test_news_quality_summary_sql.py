import os
from contextlib import contextmanager

import pytest

from services import persistent_store as store


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="Temporary-table PostgreSQL test")
@pytest.mark.parametrize("populated", [False, True])
def test_grouped_quality_summary_matches_independent_counts(monkeypatch, populated):
    with store.psycopg.connect(store.DATABASE_URL, autocommit=True,
                              row_factory=store.dict_row) as conn:
        conn.execute("SET search_path = pg_temp")
        conn.execute("""CREATE TEMP TABLE historical_article_enrichments (
            result jsonb, related_tickers jsonb, related_companies jsonb,
            company_relationships jsonb, verification_status text,
            display_headline text, original_title text, relevance_confidence float,
            relevance_label text, published_ts bigint, processed_at timestamptz)""")
        conn.execute("CREATE TEMP TABLE historical_article_retries (url text)")
        conn.execute("CREATE TEMP TABLE news_llm_validations (category text, judgment text)")
        if populated:
            for label, source, error, published, confidence, verification in [
                ("business", "canonical", False, 100, .9, "canonical"),
                ("business", "canonical", False, 200, .8, "gdelt_mention"),
                ("business", "llm_translation", False, 300, .79, "canonical"),
                ("uncertain", "llm_translation", False, 400, .95, "unknown"),
                ("unrelated", "canonical", True, 500, .99, "canonical"),
                ("", "", False, 0, .8, "canonical"),
                (None, None, False, -1, None, None),
            ]:
                result = {"entity_link_version": 2, "headline_source": source}
                if error:
                    result["processing_error"] = "failed"
                conn.execute("""INSERT INTO historical_article_enrichments VALUES
                    (%s, '["ABC"]', '["Company"]', '[]', %s, 'New', 'Old', %s, %s, %s,
                     to_timestamp(%s))""",
                    (store.jsonb(result), verification, confidence, label, published, published + 1000))
            # SQL NULL result is neither completed nor a processing error.
            conn.execute("""INSERT INTO historical_article_enrichments VALUES
                (NULL, NULL, '[]', '[{}]', NULL, '', '', 0, 'other', NULL, NULL)""")

        predicates = {
            "total": "TRUE",
            "completed": "NOT (result ? 'processing_error')",
            "processing_errors": "result ? 'processing_error'",
            "with_tickers": "jsonb_array_length(related_tickers) > 0",
            "with_companies": "jsonb_array_length(related_companies) > 0",
            "with_relationships": "jsonb_array_length(company_relationships) > 0",
            "canonical_verified": "verification_status = 'canonical'",
            "headline_changed": "COALESCE(display_headline, '') <> '' AND display_headline IS DISTINCT FROM original_title",
            "translated": "result->>'headline_source' = 'llm_translation'",
            "source_grounded": "NOT (result ? 'processing_error') AND COALESCE((result->>'entity_link_version')::integer, 0) >= 2 AND verification_status IN ('canonical', 'gdelt_mention')",
            "confidence_passed": "NOT (result ? 'processing_error') AND relevance_confidence >= 0.80",
            "automatically_accepted": "NOT (result ? 'processing_error') AND verification_status IN ('canonical', 'gdelt_mention') AND relevance_confidence >= 0.80 AND relevance_label <> 'uncertain'",
            "uncertain_records": "NOT (result ? 'processing_error') AND relevance_label = 'uncertain'",
        }
        expected = {
            key: conn.execute(f"SELECT COUNT(*) AS n FROM historical_article_enrichments WHERE {predicate}").fetchone()["n"]
            for key, predicate in predicates.items()
        }
        expected.update(conn.execute("""SELECT
            COALESCE(MIN(published_ts) FILTER (WHERE published_ts > 0), 0) AS oldest_published_ts,
            COALESCE(MAX(published_ts) FILTER (WHERE published_ts > 0), 0) AS newest_published_ts,
            COALESCE(MAX(EXTRACT(EPOCH FROM processed_at))::bigint, 0) AS last_processed_ts
            FROM historical_article_enrichments""").fetchone())
        for key, expression in (("relevance_labels", "relevance_label"),
                                ("headline_sources", "result->>'headline_source'")):
            rows = conn.execute(f"""SELECT COALESCE(NULLIF({expression}, ''), 'unknown') AS label,
                COUNT(*) AS n FROM historical_article_enrichments
                WHERE NOT (result ? 'processing_error') GROUP BY 1""").fetchall()
            expected[key] = {row["label"]: row["n"] for row in rows}

        queries = []
        plans = []

        class RecordedConnection:
            def execute(self, query, *args, **kwargs):
                queries.append(query)
                if "FROM historical_article_enrichments" in query:
                    plans.append(conn.execute("EXPLAIN (FORMAT JSON) " + query).fetchone()["QUERY PLAN"][0]["Plan"])
                elif "FROM historical_article_retries" in query:
                    assert conn.execute("SHOW enable_sort").fetchone()["enable_sort"] == "on"
                return conn.execute(query, *args, **kwargs)

        @contextmanager
        def connect(*args, **kwargs):
            with conn.transaction():
                yield RecordedConnection()

        monkeypatch.setattr(store.psycopg, "connect", connect)
        monkeypatch.setattr(store, "ensure_schema", lambda: None)
        actual = store.news_llm_quality_summary()
        historical_queries = [q for q in queries if "FROM historical_article_enrichments" in q]
        assert len(historical_queries) == 1
        plan = plans[0]
        assert plan["Strategy"] == "Hashed"

        def count_historical_scans(node):
            return int(node.get("Relation Name") == "historical_article_enrichments") + sum(
                count_historical_scans(child) for child in node.get("Plans", [])
            )

        assert count_historical_scans(plan) == 1
        assert conn.execute("SHOW enable_sort").fetchone()["enable_sort"] == "on"
        assert {key: actual[key] for key in expected} == expected
        assert actual["retry_pending"] == 0
        assert actual["coverage"]["tickers_percent"] == (
            round(expected["with_tickers"] * 100 / expected["completed"], 1)
            if expected["completed"] else 0.0
        )
