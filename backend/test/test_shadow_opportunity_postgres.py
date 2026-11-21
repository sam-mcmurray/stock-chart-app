"""Opt-in PostgreSQL lifecycle test; all test schema changes are rolled back."""
import os
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql

from services import shadow_trade_store as store


def test_expiry_covers_downtime_preserves_fresh_rows_and_allows_new_snapshots(monkeypatch):
    database_url = os.getenv("SHADOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SHADOW_TEST_DATABASE_URL is required for PostgreSQL integration")
    with psycopg.connect(database_url) as connection:
        try:
            schema = sql.Identifier("shadow_test_" + uuid.uuid4().hex)
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
            connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(schema))
            connection.execute(store.DDL)

            @contextmanager
            def connect(*args, **kwargs):
                connection.row_factory = kwargs.get("row_factory", psycopg.rows.tuple_row)
                yield connection

            monkeypatch.setattr(store.psycopg, "connect", connect)
            monkeypatch.setattr(store, "ensure_schema", lambda: None)

            def candidate(key, ts):
                return {"id": f"{key}:{ts}", "symbol": "TEST", "ts": ts,
                        "payload": {"shadow_opportunity_key": key}}

            for key, ts in [("weekend", 1), ("monitor", 99_999), ("boundary", 100_000),
                            ("fresh", 100_001), ("entered", 1), ("skipped", 1)]:
                store.upsert_shadow_opportunity(candidate(key, ts))
            for key, status in [("monitor", "monitoring"), ("entered", "entered"),
                                ("skipped", "skipped")]:
                connection.execute("UPDATE shadow_opportunities SET status=%s WHERE opportunity_key=%s",
                                   (status, key))

            assert store.expire_stale_opportunities(expire_before_ts=100_000) == 2
            assert store.expire_stale_opportunities(expire_before_ts=100_000) == 0
            rows = {row["opportunity_key"]: row for row in store.list_opportunities()}
            assert rows["weekend"]["status"] == "expired"
            assert rows["monitor"]["status"] == "expired"
            assert rows["boundary"]["status"] == "watching"
            assert rows["fresh"]["status"] == "watching"
            assert rows["entered"]["status"] == "entered"
            assert rows["skipped"]["status"] == "skipped"
            assert rows["weekend"]["check_count"] == 0

            # A resumed Scanner can revive an expired thesis with current evidence.
            store.upsert_shadow_opportunity(candidate("weekend", 200_000))
            assert store.expire_stale_opportunities(expire_before_ts=100_000) == 0
            resumed = store.upsert_shadow_opportunity(candidate("weekend", 200_001))
            assert resumed["status"] == "watching"
            assert resumed["first_signal_ts"] == 1
            assert resumed["latest_signal_ts"] == 200_001
        finally:
            connection.rollback()
