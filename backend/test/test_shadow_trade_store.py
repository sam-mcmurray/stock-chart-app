from unittest.mock import MagicMock

from services import shadow_trade_store


def test_candidate_queue_prioritizes_new_then_least_recently_checked(monkeypatch):
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(shadow_trade_store.psycopg, "connect", connect)

    result = shadow_trade_store.list_unprocessed_candidates(since_ts=1_000, limit=100)

    assert result == []
    query = connection.execute.call_args.args[0]
    assert "(e.candidate_id IS NULL) DESC" in query
    assert "e.evaluated_at ASC NULLS FIRST" in query
    assert connection.execute.call_args.args[1] == {"since_ts": 1_000, "limit": 100}


def test_opportunity_queue_only_rechecks_changed_or_monitoring_rows(monkeypatch):
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(shadow_trade_store, "ensure_schema", MagicMock())
    monkeypatch.setattr(shadow_trade_store.psycopg, "connect", connect)

    result = shadow_trade_store.list_pending_opportunities(
        since_ts=2_000, expire_before_ts=2_900, limit=50
    )

    assert result == []
    query = connection.execute.call_args.args[0]
    assert "status IN ('watching', 'monitoring')" in query
    assert "last_evaluated_candidate_id IS DISTINCT FROM latest_candidate_id" in query
    assert "status = 'monitoring'" in query
    assert "latest_signal_ts < %(expire_before_ts)s" in query
    assert connection.execute.call_args.args[1] == {
        "since_ts": 2_000, "expire_before_ts": 2_900, "limit": 50,
    }


def test_opportunity_identity_is_upserted_from_candidate_payload(monkeypatch):
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {
        "opportunity_key": "shadow-opportunity:abc",
        "latest_candidate_id": "candidate-2",
    }
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(shadow_trade_store, "ensure_schema", MagicMock())
    monkeypatch.setattr(shadow_trade_store.psycopg, "connect", connect)

    result = shadow_trade_store.upsert_shadow_opportunity({
        "id": "candidate-2", "symbol": "nvda", "ts": 2_100,
        "payload": {"shadow_opportunity_key": "shadow-opportunity:abc"},
    })

    assert result["latest_candidate_id"] == "candidate-2"
    query = connection.execute.call_args.args[0]
    assert "ON CONFLICT (opportunity_key) DO UPDATE" in query
    params = connection.execute.call_args.args[1]
    assert params["key"] == "shadow-opportunity:abc"
    assert params["symbol"] == "NVDA"
