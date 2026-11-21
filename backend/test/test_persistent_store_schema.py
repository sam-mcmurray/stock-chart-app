from unittest.mock import MagicMock
from contextlib import contextmanager
import os
import pytest

from services import persistent_store


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="Temporary-table PostgreSQL test")
def test_known_additive_graph_migration_does_not_repeat_baseline_ddl(monkeypatch):
    with persistent_store.psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE worker_checkpoints (name text PRIMARY KEY, value jsonb, updated_at timestamptz)")

        @contextmanager
        def connect(*args, **kwargs):
            with conn.transaction():
                yield conn

        monkeypatch.setattr(persistent_store.psycopg, "connect", connect)
        monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
        baseline = "CREATE TEMP TABLE migration_baseline_probe (id int);\n"
        addition = "CREATE TEMP TABLE migration_outbox_probe (url text);\n"
        monkeypatch.setattr(persistent_store, "_GRAPH_OUTBOX_SCHEMA", addition)
        monkeypatch.setattr(persistent_store, "SCHEMA", baseline)
        persistent_store.ensure_schema()
        monkeypatch.setattr(persistent_store, "SCHEMA", baseline + addition)
        persistent_store.ensure_schema()
        assert conn.execute("SELECT count(*) FROM migration_outbox_probe").fetchone()[0] == 0
        monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
        persistent_store.ensure_schema()


@pytest.mark.skipif(os.getenv("DRL_SQL_INTEGRATION") != "1", reason="Temporary-table PostgreSQL test")
def test_schema_checkpoint_transaction_and_restart_postgres(monkeypatch):
    with persistent_store.psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE worker_checkpoints (name text PRIMARY KEY, value jsonb, updated_at timestamptz)")

        @contextmanager
        def connect(*args, **kwargs):
            with conn.transaction():
                yield conn

        monkeypatch.setattr(persistent_store.psycopg, "connect", connect)
        monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
        schema = "CREATE TEMP TABLE schema_checkpoint_probe (id int)"
        monkeypatch.setattr(persistent_store, "SCHEMA", schema)
        persistent_store.ensure_schema()
        saved = conn.execute("SELECT value FROM worker_checkpoints").fetchone()[0]
        # A new process must skip this non-idempotent DDL based on the committed marker.
        monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
        persistent_store.ensure_schema()
        monkeypatch.setattr(persistent_store, "SCHEMA", "ALTER TABLE schema_checkpoint_probe ADD COLUMN extra int; SELECT 1/0")
        with pytest.raises(persistent_store.psycopg.errors.DivisionByZero):
            persistent_store.ensure_schema()
        assert conn.execute("SELECT value FROM worker_checkpoints").fetchone()[0] == saved
        monkeypatch.setattr(persistent_store, "SCHEMA", "ALTER TABLE schema_checkpoint_probe ADD COLUMN extra int")
        persistent_store.ensure_schema()
        assert conn.execute("SELECT value FROM worker_checkpoints").fetchone()[0] != saved


def test_schema_ddl_is_serialized_with_transaction_advisory_lock(monkeypatch):
    monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
    conn = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(
        persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    persistent_store.ensure_schema()

    assert conn.execute.call_args_list[0].args[0] == (
        "SET LOCAL lock_timeout = '5s'"
    )
    assert conn.execute.call_args_list[1].args[0] == (
        "SELECT pg_advisory_xact_lock(741852963)"
    )
    assert conn.execute.call_args_list[4].args[0] == persistent_store.SCHEMA
    persistent_store.ensure_schema()
    assert conn.execute.call_count == 6


@pytest.mark.parametrize("stored", ["matching", "outdated", "missing", "new_database"])
def test_schema_checkpoint_skips_only_committed_matching_schema(monkeypatch, stored):
    monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    conn = MagicMock()
    digest = persistent_store.hashlib.sha256(persistent_store.SCHEMA.encode()).hexdigest()
    values = [(stored != "new_database",)]
    if stored != "new_database":
        values.append((digest,) if stored == "matching" else ("old",) if stored == "outdated" else None)
    conn.execute.return_value.fetchone.side_effect = values
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(persistent_store.psycopg, "connect", MagicMock(return_value=context))

    persistent_store.ensure_schema()

    statements = [call.args[0] for call in conn.execute.call_args_list]
    assert (persistent_store.SCHEMA in statements) == (stored != "matching")
    assert any(sql.startswith("INSERT INTO worker_checkpoints") for sql in statements) == (stored != "matching")
    assert persistent_store._SCHEMA_READY_FOR == (persistent_store.DATABASE_URL, persistent_store.SCHEMA)


def test_schema_lock_timeout_rolls_back_without_caching_success(monkeypatch):
    monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    context = MagicMock()
    context.__exit__.return_value = False
    conn = context.__enter__.return_value
    monkeypatch.setattr(persistent_store.psycopg, "connect", MagicMock(return_value=context))
    conn.execute.side_effect = [None, None, persistent_store.psycopg.errors.LockNotAvailable()]
    with pytest.raises(persistent_store.psycopg.errors.LockNotAvailable):
        persistent_store.ensure_schema()
    assert persistent_store._SCHEMA_READY_FOR is None
    assert context.__exit__.call_args.args[0] is persistent_store.psycopg.errors.LockNotAvailable
    conn.execute.side_effect = None
    persistent_store.ensure_schema()
    assert persistent_store._SCHEMA_READY_FOR == (persistent_store.DATABASE_URL, persistent_store.SCHEMA)


def test_schema_failure_is_retried_and_database_change_rechecks(monkeypatch):
    monkeypatch.setattr(persistent_store, "_SCHEMA_READY_FOR", None)
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    context = MagicMock()
    context.__exit__.side_effect = [RuntimeError("commit failed"), None, None]
    connect = MagicMock(return_value=context)
    monkeypatch.setattr(persistent_store.psycopg, "connect", connect)
    with pytest.raises(RuntimeError, match="commit failed"):
        persistent_store.ensure_schema()
    assert persistent_store._SCHEMA_READY_FOR is None
    persistent_store.ensure_schema()
    persistent_store.ensure_schema()
    assert connect.call_count == 2
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://other")
    persistent_store.ensure_schema()
    assert connect.call_count == 3


def test_healthcheck_requires_reachable_trade_journal(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (True,)
    context = MagicMock()
    context.__enter__.return_value = conn
    connect = MagicMock(return_value=context)
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(persistent_store.psycopg, "connect", connect)

    assert persistent_store.healthcheck() is True
    connect.assert_called_once_with("postgresql://test", connect_timeout=2)
    conn.execute.assert_called_once_with(
        "SELECT to_regclass('public.trade_positions') IS NOT NULL"
    )


def test_gdelt_mention_batch_loader_is_bounded(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = [{"event_id": "123", "mention_ts": 1}]
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(persistent_store, "ensure_schema", MagicMock())
    monkeypatch.setattr(
        persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    rows = persistent_store.list_gdelt_mentions_for_events(
        ["456", "123", "123", "not-an-id"], limit=10_000
    )

    assert rows == [{"event_id": "123", "mention_ts": 1}]
    params = conn.execute.call_args.args[1]
    assert params == {"event_ids": ["123", "456"], "limit": 5000}


def test_news_enrichment_version_identity_ignores_observation_time():
    first = persistent_store._news_enrichment_version(
        record_type="historical_article",
        record_id="https://example.com/a",
        article_url="https://example.com/a",
        published_ts=100,
        observed_ts=200,
        pipeline_version="v1",
        result={"related_tickers": ["NVDA"]},
    )
    repeated = persistent_store._news_enrichment_version(
        record_type="historical_article",
        record_id="https://example.com/a",
        article_url="https://example.com/a",
        published_ts=100,
        observed_ts=300,
        pipeline_version="v1",
        result={"related_tickers": ["NVDA"]},
    )
    revised = persistent_store._news_enrichment_version(
        record_type="historical_article",
        record_id="https://example.com/a",
        article_url="https://example.com/a",
        published_ts=100,
        observed_ts=300,
        pipeline_version="v1",
        result={"related_tickers": ["AMD"]},
    )

    assert first["version_key"] == repeated["version_key"]
    assert revised["version_key"] != first["version_key"]


def test_market_observation_query_applies_event_and_receipt_cutoffs(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(persistent_store, "ensure_schema", MagicMock())
    monkeypatch.setattr(
        persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    persistent_store.list_market_observations_as_of(
        "nvda", as_of_ts=500, start_ts=100
    )

    sql, params = conn.execute.call_args.args
    assert "event_ts BETWEEN %(start_ts)s AND %(as_of_ts)s" in sql
    assert "received_ts <= %(as_of_ts)s" in sql
    assert params["symbol"] == "NVDA"
    assert params["as_of_ts"] == 500


def test_replay_market_query_returns_raw_revisions_with_receipt_cutoff(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(persistent_store, "ensure_schema", MagicMock())
    monkeypatch.setattr(
        persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    persistent_store.list_market_observation_versions(
        "nvda", start_ts=100, end_ts=500
    )

    sql, params = conn.execute.call_args.args
    assert "received_ts <= %(end_ts)s" in sql
    assert "DISTINCT" not in sql
    assert params["symbol"] == "NVDA"


def test_replay_market_query_can_load_all_observation_types(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(persistent_store, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(persistent_store, "ensure_schema", MagicMock())
    monkeypatch.setattr(
        persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    persistent_store.list_market_observation_versions(
        "nvda", start_ts=100, end_ts=500, observation_type=None
    )

    sql, params = conn.execute.call_args.args
    assert "AND observation_type =" not in sql
    assert params["observation_type"] is None
