from unittest.mock import MagicMock

import pytest

from services import trade_journal


def test_reduce_long_position_fills_handles_partial_entry_replay():
    fill = {"instruction": "BUY", "quantity": 25, "price": 10}

    result = trade_journal.reduce_long_position_fills([fill], intended_quantity=100)

    assert result["state"] == "entry_pending"
    assert result["filled_quantity"] == 25
    assert result["remaining_quantity"] == 25


def test_reduce_long_position_fills_handles_partial_profit_and_realized_pnl():
    result = trade_journal.reduce_long_position_fills(
        [
            {"instruction": "BUY", "quantity": 40, "price": 10},
            {"instruction": "BUY", "quantity": 60, "price": 11},
            {"instruction": "SELL", "quantity": 50, "price": 13},
        ],
        intended_quantity=100,
    )

    assert result["state"] == "partial_taken"
    assert result["average_entry_price"] == 10.6
    assert result["remaining_quantity"] == 50
    assert result["realized_pnl"] == pytest.approx(120)


def test_reduce_long_position_fills_closes_without_double_counting_excess_sell():
    result = trade_journal.reduce_long_position_fills(
        [
            {"instruction": "BUY", "quantity": 10, "price": 5},
            {"instruction": "SELL", "quantity": 20, "price": 6},
        ],
        intended_quantity=10,
    )

    assert result["state"] == "closed"
    assert result["remaining_quantity"] == 0
    assert result["realized_pnl"] == 10


def test_reduce_long_position_fills_blocks_terminal_unfilled_entry():
    result = trade_journal.reduce_long_position_fills(
        [],
        intended_quantity=10,
        entry_terminal_without_fill=True,
    )

    assert result["state"] == "blocked"


def test_validate_idempotency_key_rejects_missing_or_unsafe_values():
    with pytest.raises(ValueError):
        trade_journal.validate_idempotency_key(None)
    with pytest.raises(ValueError):
        trade_journal.validate_idempotency_key("short")
    with pytest.raises(ValueError):
        trade_journal.validate_idempotency_key("order key with spaces")


def test_reserve_order_intent_returns_new_reservation(monkeypatch):
    row = {
        "idempotency_key": "entry:AAPL:123",
        "account_id": "account",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "order_type": "LIMIT",
        "status": "reserved",
    }
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    result = trade_journal.reserve_order_intent(
        idempotency_key="entry:AAPL:123",
        account_id="account",
        symbol="aapl",
        side="buy",
        quantity=10,
        order_type="limit",
        order_payload={"price": 10},
    )

    assert result.created is True
    assert result.intent["status"] == "reserved"
    assert conn.execute.call_count == 1


def test_reserve_order_intent_replays_identical_existing_intent(monkeypatch):
    existing = {
        "idempotency_key": "entry:AAPL:123",
        "account_id": "account",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "order_type": "LIMIT",
        "status": "submitted",
        "broker_order_id": 77,
    }
    insert_cursor = MagicMock()
    insert_cursor.fetchone.return_value = None
    select_cursor = MagicMock()
    select_cursor.fetchone.return_value = existing
    conn = MagicMock()
    conn.execute.side_effect = [insert_cursor, select_cursor]
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    result = trade_journal.reserve_order_intent(
        idempotency_key="entry:AAPL:123",
        account_id="account",
        symbol="AAPL",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        order_payload={"price": 10},
    )

    assert result.created is False
    assert result.intent["broker_order_id"] == 77


def test_reserve_order_intent_rejects_conflicting_reuse(monkeypatch):
    existing = {
        "account_id": "account",
        "symbol": "MSFT",
        "side": "BUY",
        "quantity": 10,
        "order_type": "LIMIT",
    }
    insert_cursor = MagicMock()
    insert_cursor.fetchone.return_value = None
    select_cursor = MagicMock()
    select_cursor.fetchone.return_value = existing
    conn = MagicMock()
    conn.execute.side_effect = [insert_cursor, select_cursor]
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    with pytest.raises(trade_journal.IdempotencyConflict):
        trade_journal.reserve_order_intent(
            idempotency_key="entry:AAPL:123",
            account_id="account",
            symbol="AAPL",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            order_payload={"price": 10},
        )


def test_reserve_order_intent_rejects_unknown_role(monkeypatch):
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)

    with pytest.raises(ValueError, match="intent role"):
        trade_journal.reserve_order_intent(
            idempotency_key="entry:AAPL:123",
            account_id="account",
            symbol="AAPL",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            order_payload={"price": 10},
            intent_role="untrusted-role",
        )


def test_list_order_intents_is_bounded_and_serializable(monkeypatch):
    from datetime import datetime, timezone

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {
            "idempotency_key": "entry:AAPL:123",
            "symbol": "AAPL",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        }
    ]
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    rows = trade_journal.list_order_intents(account_id="account", limit=999)

    assert rows[0]["created_at"] == "2026-01-01T00:00:00+00:00"
    assert conn.execute.call_args.args[1] == {"account_id": "account", "limit": 200}


def test_list_reconcilable_orders_requires_broker_identity(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {"idempotency_key": "protect:abc", "broker_order_id": 77, "status": "cancel_pending"}
    ]
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        trade_journal.persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    rows = trade_journal.list_reconcilable_order_intents(limit=999)

    assert rows[0]["broker_order_id"] == 77
    sql = conn.execute.call_args.args[0]
    assert "broker_order_id IS NOT NULL" in sql
    assert "cancel_pending" in sql
    assert "last_broker_payload IS NULL" in sql
    assert "'filled', 'canceled', 'rejected', 'failed'" in sql
    assert conn.execute.call_args.args[1]["limit"] == 500


def test_list_unidentified_orders_only_returns_ambiguous_without_broker_id(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = [{"idempotency_key": "exit:abcdefgh", "status": "unknown"}]
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        trade_journal.persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    rows = trade_journal.list_unidentified_order_intents(limit=999)

    assert rows[0]["status"] == "unknown"
    sql = conn.execute.call_args.args[0]
    assert "broker_order_id IS NULL" in sql
    assert "'submitting', 'unknown'" in sql
    assert conn.execute.call_args.args[1]["limit"] == 500


def test_attach_broker_identity_is_guarded_against_cross_intent_claims(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "idempotency_key": "exit:abcdefgh",
        "broker_order_id": 77,
    }
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        trade_journal.persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    row = trade_journal.attach_broker_order_identity(
        "exit:abcdefgh", broker_order_id=77
    )

    assert row["broker_order_id"] == 77
    sql = conn.execute.call_args.args[0]
    assert "target.broker_order_id IS NULL" in sql
    assert "NOT EXISTS" in sql


def test_claim_order_submission_is_compare_and_set(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "idempotency_key": "exit:abcdefgh",
        "status": "submitting",
    }
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        trade_journal.persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    result = trade_journal.claim_order_intent_submission("exit:abcdefgh")

    assert result["status"] == "submitting"
    sql = conn.execute.call_args.args[0]
    assert "status = 'reserved'" in sql
    assert "status = 'submitting'" in sql


def test_record_account_activity_is_idempotent_and_updates_matching_order(monkeypatch):
    insert_cursor = MagicMock()
    insert_cursor.fetchone.return_value = {"event_key": "a" * 64}
    update_cursor = MagicMock()
    update_cursor.fetchall.return_value = [{"idempotency_key": "entry:AAPL:123"}]
    conn = MagicMock()
    conn.execute.side_effect = [insert_cursor, update_cursor]
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    result = trade_journal.record_account_activity({
        "event_key": "a" * 64,
        "account_number": "123",
        "sequence_number": 8,
        "message_type": "ORDER",
        "broker_order_id": 77,
        "order_status": "filled",
        "event_ts": 100,
        "payload": {},
    })

    assert result == {"event_key": "a" * 64, "created": True, "matched_intents": 1}
    update_sql = conn.execute.call_args_list[1].args[0]
    assert "status NOT IN ('filled', 'canceled', 'rejected', 'failed')" in update_sql


def test_record_duplicate_account_activity_does_not_reapply_state(monkeypatch):
    insert_cursor = MagicMock()
    insert_cursor.fetchone.return_value = None
    conn = MagicMock()
    conn.execute.return_value = insert_cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    result = trade_journal.record_account_activity({
        "event_key": "b" * 64,
        "message_type": "NOTICE",
        "event_ts": 100,
        "payload": {},
    })

    assert result["created"] is False
    assert result["matched_intents"] == 0
    assert conn.execute.call_count == 1


def test_record_order_snapshot_inserts_each_fill_once(monkeypatch):
    intent_cursor = MagicMock()
    intent_cursor.fetchone.return_value = {"idempotency_key": "entry:AAPL:123"}
    update_cursor = MagicMock()
    update_cursor.fetchone.return_value = {"position_id": None}
    fill_cursor = MagicMock()
    fill_cursor.fetchone.return_value = {"fill_key": "c" * 64}
    conn = MagicMock()
    conn.execute.side_effect = [intent_cursor, update_cursor, fill_cursor]
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    result = trade_journal.record_order_snapshot({
        "broker_order_id": 77,
        "status": "partially_filled",
        "filled_quantity": 5,
        "remaining_quantity": 5,
        "average_fill_price": 10.25,
        "raw": {"orderId": 77},
        "fills": [{
            "fill_key": "c" * 64,
            "broker_order_id": 77,
            "leg_id": 1,
            "symbol": "AAPL",
            "instruction": "BUY",
            "quantity": 5,
            "price": 10.25,
            "executed_at": "2026-01-01T15:00:00Z",
        }],
    })

    assert result["matched"] is True
    assert result["fills_created"] == 1
    assert conn.execute.call_count == 3


def test_reordered_snapshot_cannot_regress_terminal_order_state():
    merged = trade_journal.merge_order_snapshot(
        {
            "status": "rejected",
            "quantity": 100,
            "filled_quantity": 0,
            "remaining_quantity": 100,
            "average_fill_price": 0,
            "last_broker_payload": {"status": "REJECTED"},
        },
        {
            "status": "submitted",
            "filled_quantity": 0,
            "remaining_quantity": 100,
            "average_fill_price": 0,
            "raw": {"status": "WORKING"},
        },
    )

    assert merged == {
        "status": "rejected",
        "filled_quantity": 0,
        "remaining_quantity": 100,
        "average_fill_price": 0,
        "raw": {"status": "REJECTED"},
    }


def test_reordered_snapshot_cannot_lose_fill_progress():
    merged = trade_journal.merge_order_snapshot(
        {
            "status": "filled",
            "quantity": 100,
            "filled_quantity": 100,
            "remaining_quantity": 0,
            "average_fill_price": 10.25,
            "last_broker_payload": {"status": "FILLED"},
        },
        {
            "status": "partially_filled",
            "filled_quantity": 25,
            "remaining_quantity": 75,
            "average_fill_price": 10,
            "raw": {"status": "PARTIALLY_FILLED"},
        },
    )

    assert merged["status"] == "filled"
    assert merged["filled_quantity"] == 100
    assert merged["remaining_quantity"] == 0
    assert merged["average_fill_price"] == 10.25
    assert merged["raw"] == {"status": "FILLED"}


def test_fill_wins_a_cancel_race_and_preserves_cumulative_quantity():
    merged = trade_journal.merge_order_snapshot(
        {
            "status": "canceled",
            "quantity": 100,
            "filled_quantity": 20,
            "remaining_quantity": 80,
            "average_fill_price": 10,
        },
        {
            "status": "filled",
            "filled_quantity": 100,
            "remaining_quantity": 0,
            "average_fill_price": 10.5,
            "raw": {"status": "FILLED"},
        },
    )

    assert merged["status"] == "filled"
    assert merged["filled_quantity"] == 100
    assert merged["remaining_quantity"] == 0
    assert merged["average_fill_price"] == 10.5


def test_ensure_entry_position_attaches_one_deterministic_position(monkeypatch):
    intent_cursor = MagicMock()
    intent_cursor.fetchone.return_value = {"idempotency_key": "entry:AAPL:123", "position_id": None}
    active_cursor = MagicMock()
    active_cursor.fetchone.return_value = None
    conn = MagicMock()
    conn.execute.side_effect = [intent_cursor, active_cursor, MagicMock(), MagicMock()]
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    position_id = trade_journal.ensure_entry_position(
        idempotency_key="entry:AAPL:123",
        account_id="encrypted-account",
        symbol="aapl",
        intended_quantity=100,
        hard_stop_price=9.50,
        strategy_version="test-v1",
    )

    assert position_id.startswith("pos:")
    assert len(position_id) == 68
    assert conn.execute.call_count == 4
    insert_params = conn.execute.call_args_list[2].args[1]
    assert insert_params["position_id"] == position_id
    assert insert_params["hard_stop_price"] == 9.50


def test_ensure_entry_position_returns_existing_attachment(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = {"position_id": "pos:existing"}
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_journal.persistent_store.psycopg, "connect", MagicMock(return_value=context))

    position_id = trade_journal.ensure_entry_position(
        idempotency_key="entry:AAPL:123",
        account_id="encrypted-account",
        symbol="AAPL",
        intended_quantity=100,
        hard_stop_price=9.50,
        strategy_version="test-v1",
    )

    assert position_id == "pos:existing"
    assert conn.execute.call_count == 1


def test_update_position_risk_state_ratchets_peak_and_stop(monkeypatch):
    source = {
        "position_id": "pos:abc",
        "remaining_quantity": 50.0,
        "average_entry_price": 10.0,
        "hard_stop_price": 9.0,
        "active_stop_price": 9.5,
        "peak_price": 11.0,
    }
    updated = {**source, "active_stop_price": 10.0, "peak_price": 12.0}
    source_cursor = MagicMock()
    source_cursor.fetchone.return_value = source
    update_cursor = MagicMock()
    update_cursor.fetchone.return_value = updated
    conn = MagicMock()
    conn.execute.side_effect = [source_cursor, update_cursor, MagicMock()]
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        trade_journal.persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    result = trade_journal.update_position_risk_state(
        position_id="pos:abc",
        expected_remaining_quantity=50,
        peak_price=12,
        active_stop_price=10,
        decision={"action": "hold", "reason": None},
        event_ts=123,
    )

    assert result["updated"] is True
    update_params = conn.execute.call_args_list[1].args[1]
    assert update_params["peak_price"] == 12
    assert update_params["active_stop_price"] == 10
    assert "risk_decision" in conn.execute.call_args_list[2].args[0]


def test_update_position_risk_state_rejects_stale_quantity(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "position_id": "pos:abc",
        "remaining_quantity": 25.0,
        "average_entry_price": 10.0,
        "hard_stop_price": 9.0,
        "active_stop_price": 9.5,
        "peak_price": 11.0,
    }
    conn = MagicMock()
    conn.execute.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(trade_journal.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(trade_journal.persistent_store, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        trade_journal.persistent_store.psycopg,
        "connect",
        MagicMock(return_value=context),
    )

    result = trade_journal.update_position_risk_state(
        position_id="pos:abc",
        expected_remaining_quantity=50,
        peak_price=12,
        active_stop_price=10,
        decision={"action": "take_partial", "reason": "partial_profit_target"},
        event_ts=123,
    )

    assert result["updated"] is False
    assert result["reason"] == "position_quantity_changed"
    assert conn.execute.call_count == 1
