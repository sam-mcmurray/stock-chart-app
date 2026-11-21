import os
from contextlib import contextmanager

import pytest

from services import persistent_store as store


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="Temporary-table PostgreSQL test")
def test_retry_seed_pages_resume_and_commit_atomically(monkeypatch):
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE worker_checkpoints (name text PRIMARY KEY, value jsonb, updated_at timestamptz)")
        conn.execute("CREATE TEMP TABLE historical_article_enrichments (url text PRIMARY KEY, original_title text, source text, published_ts bigint, result jsonb, pipeline_version text)")
        conn.execute("CREATE TEMP TABLE historical_article_retries (url text PRIMARY KEY, article jsonb, pipeline_version text, attempts int, last_error text, next_attempt_at timestamptz)")
        conn.execute("""INSERT INTO historical_article_enrichments VALUES
            ('a','A','source',1,'{}','v1'),
            ('b','B','source',2,'{"processing_error":"bad JSON"}','v1'),
            ('c','C','source',3,'{"processing_error":"other version"}','v2'),
            ('d','D','source',4,'{"processing_error":"timeout"}','v1')""")

        @contextmanager
        def connect(*args, **kwargs):
            with conn.transaction():
                yield conn

        monkeypatch.setattr(store.psycopg, "connect", connect)
        monkeypatch.setattr(store, "ensure_schema", lambda: None)
        first = store._seed_historical_article_retry_page("v1", limit=2)
        assert first == {"through_url": "d", "after_url": "b", "seeded": 1, "scanned": 2, "complete": False}
        # A later insertion belongs to the regular retry path, not this frozen migration.
        conn.execute("INSERT INTO historical_article_enrichments VALUES ('z','Z','source',5,'{}','v1')")
        conn.execute("ALTER TABLE historical_article_retries ADD CHECK (url <> 'd')")
        with pytest.raises(psycopg.errors.CheckViolation):
            store._seed_historical_article_retry_page("v1", limit=2)
        assert conn.execute("SELECT value FROM worker_checkpoints").fetchone()[0] == first
        conn.execute("ALTER TABLE historical_article_retries DROP CONSTRAINT historical_article_retries_url_check")
        last = store._seed_historical_article_retry_page("v1", limit=2)
        assert last["complete"] and last["scanned"] == 4 and last["seeded"] == 2
        assert conn.execute("SELECT url FROM historical_article_retries ORDER BY url").fetchall() == [('b',), ('d',)]
        # Completed runs do not read the source again, even after process restart.
        conn.execute("DROP TABLE historical_article_enrichments")
        assert store._seed_historical_article_retry_page("v1") == last
