import json
import os
from contextlib import contextmanager

import pytest

from services import historical_candidate_generator as generator


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="Temporary-table PostgreSQL test")
def test_candidate_index_preserves_selection_and_supports_lag_filter(monkeypatch):
    with generator.psycopg.connect(os.environ["DATABASE_URL"], autocommit=True,
                                  row_factory=generator.dict_row) as conn:
        conn.execute("SET search_path = pg_temp")
        conn.execute("""CREATE TEMP TABLE news_enrichment_versions (
            version_key text PRIMARY KEY, record_type text, record_id text,
            article_url text, published_ts bigint, observed_ts bigint,
            pipeline_version text, result jsonb)""")
        conn.execute("""CREATE TEMP TABLE historical_candidate_source_versions (
            version_key text, generator_version text, status text, next_retry_at timestamptz,
            PRIMARY KEY (version_key, generator_version))""")
        accepted = {"relevance_label": "business", "article_verification_status": "publisher",
                    "entity_link_version": 2, "related_tickers": ["NVDA"]}
        for key, published, observed, changes in [
            ("new-old", 190, 200, {}), ("new-recent", 290, 300, {}),
            ("due", 390, 400, {}), ("future-retry", 490, 500, {}),
            ("done", 590, 600, {}), ("too-late", 100, 1000, {}),
            ("future-published", 1000, 900, {}),
            ("unverified", 690, 700, {"article_verification_status": "unknown"}),
            ("irrelevant", 690, 700, {"relevance_label": "unrelated"}),
            ("no-tickers", 690, 700, {"related_tickers": []}),
            ("old-link", 690, 700, {"entity_link_version": 1}),
        ]:
            conn.execute("INSERT INTO news_enrichment_versions VALUES (%s,'article',%s,'url',%s,%s,'v1',%s)",
                         (key, key, published, observed, generator.persistent_store.jsonb({**accepted, **changes})))
        conn.execute("""INSERT INTO historical_candidate_source_versions VALUES
            ('due',%s,'error',NULL), ('future-retry',%s,'error',NOW()+INTERVAL '1 day'),
            ('done',%s,'done',NULL)""", (generator.GENERATOR_VERSION,) * 3)

        @contextmanager
        def connect(*args, **kwargs):
            with conn.transaction():
                yield conn

        monkeypatch.setattr(generator.psycopg, "connect", connect)
        before = {version: generator.list_pending_sources(maximum_lag_seconds=60, limit=100,
                  generator_version=version) for version in generator.GENERATOR_SPECS}
        assert [row["version_key"] for row in before[generator.GENERATOR_VERSION]] == ["new-recent", "new-old", "due"]
        conn.execute(generator.CANDIDATE_LOOKUP_INDEX_SQL)
        conn.execute("SET enable_seqscan = off")
        after = {version: generator.list_pending_sources(maximum_lag_seconds=60, limit=100,
                 generator_version=version) for version in generator.GENERATOR_SPECS}
        assert after == before
        plan = conn.execute("""EXPLAIN (FORMAT JSON) SELECT version_key FROM news_enrichment_versions
            WHERE published_ts > 0 AND observed_ts >= published_ts
              AND observed_ts - published_ts <= %s
              AND COALESCE((result->>'entity_link_version')::integer,0) >= 2
              AND jsonb_array_length(COALESCE(result->'related_tickers','[]'::jsonb)) > 0""", (60,)).fetchone()
        assert "idx_news_enrichment_versions_candidate_lag" in json.dumps(plan)
