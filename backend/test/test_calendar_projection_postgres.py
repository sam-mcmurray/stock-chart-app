import os
from contextlib import contextmanager

import pytest

from services import persistent_store as store


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="Temporary-table PostgreSQL test")
def test_calendar_refresh_preserves_ai_projection(monkeypatch):
    with store.psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("SET search_path = pg_temp")
        conn.execute("CREATE TEMP TABLE worker_checkpoints (name text PRIMARY KEY, value jsonb, updated_at timestamptz)")
        conn.execute("""CREATE TEMP TABLE calendar_events (
            id text PRIMARY KEY, event_date date, event_type text, title text,
            source text, url text, symbols jsonb, confidence float,
            extraction_source text, details jsonb, updated_at timestamptz
        )""")

        @contextmanager
        def connect(*args, **kwargs):
            with conn.transaction():
                yield conn

        monkeypatch.setattr(store.psycopg, "connect", connect)
        monkeypatch.setattr(store, "ensure_schema", lambda: None)
        base = dict(id="news", date="2026-10-17", type="Market note", title="Raw title",
                    source="test", url="https://example.com", symbols=["WRONG"],
                    confidence=0.9, extraction_source="test")
        enriched = {**base, "details": {"calendar_kind": "market_news",
                    "display_headline": "Verified headline", "related_tickers": ["NVDA", "MSFT"],
                    "summary": "old summary"}}
        store.upsert_calendar_events([enriched])
        assert conn.execute("SELECT title, symbols FROM calendar_events").fetchone() == (
            "Verified headline", ["NVDA", "MSFT"])
        # Repeated provider refreshes update raw metadata without undoing enrichment.
        refresh = {**base, "details": {"calendar_kind": "market_news", "summary": "new summary"}}
        store.upsert_calendar_events([refresh])
        row = conn.execute("SELECT title, symbols, details FROM calendar_events").fetchone()
        assert row[:2] == ("Verified headline", ["NVDA", "MSFT"])
        assert row[2]["summary"] == "new summary"
        assert enriched["symbols"] == ["WRONG"]  # Caller input is not mutated.
        # No verified ticker means no raw provider ticker leaks into market news.
        store.upsert_calendar_events([{**refresh, "id": "unclassified"}])
        assert conn.execute("SELECT symbols FROM calendar_events WHERE id='unclassified'").fetchone()[0] == []
        # Non-news calendar entries keep their ordinary symbols and titles.
        store.upsert_calendar_events([{**base, "id": "ipo", "details": {"calendar_kind": "ipo"}}])
        store.upsert_calendar_events([{**base, "id": "ipo", "title": "IPO updated", "symbols": ["IPO"], "details": {"calendar_kind": "ipo"}}])
        assert conn.execute("SELECT title, symbols FROM calendar_events WHERE id='ipo'").fetchone() == ("IPO updated", ["IPO"])
        conn.execute("UPDATE calendar_events SET title='legacy' WHERE id='news'")
        conn.execute("ALTER TABLE calendar_events ADD CONSTRAINT repair_failure CHECK (id <> 'news' OR title='legacy')")
        with pytest.raises(store.psycopg.errors.CheckViolation):
            store.normalize_calendar_news_ai_projection()
        assert conn.execute("SELECT count(*) FROM worker_checkpoints").fetchone()[0] == 0
        conn.execute("ALTER TABLE calendar_events DROP CONSTRAINT repair_failure")
        assert store.normalize_calendar_news_ai_projection() == 1
        assert conn.execute("SELECT title FROM calendar_events WHERE id='news'").fetchone()[0] == "Verified headline"
        conn.execute("DROP TABLE calendar_events")
        # A completed migration does not even touch the source table on restart.
        assert store.normalize_calendar_news_ai_projection() == 0
