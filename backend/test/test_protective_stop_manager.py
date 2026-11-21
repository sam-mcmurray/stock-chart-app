from unittest.mock import MagicMock, call

import pytest

from services import protective_stop_manager
from services.trade_journal import OrderIntentReservation


def _position(**updates):
    return {
        "position_id": "pos:abc",
        "account_id": "encrypted-account",
        "symbol": "ABCD",
        "state": "open",
        "remaining_quantity": 100,
        "average_entry_price": 10,
        "hard_stop_price": 9.47,
        "active_stop_price": 9.47,
        **updates,
    }


def _intent(**updates):
    return {
        "idempotency_key": "protect:old12345",
        "position_id": "pos:abc",
        "status": "submitted",
        "broker_order_id": 77,
        "quantity": 100,
        "order_payload": {"stopPrice": 9.47},
        **updates,
    }


def test_dry_run_reports_missing_stop_without_journal_or_broker_calls(monkeypatch):
    reserve = MagicMock()
    monkeypatch.setattr(protective_stop_manager, "reserve_order_intent", reserve)
    client = MagicMock()

    result = protective_stop_manager.synchronize_protective_stop(
        position=_position(), protective_intent=None, order_client=client, execute=False
    )

    assert result["decision"]["action"] == "create"
    assert result["order_payload"]["orderType"] == "STOP"
    assert result["executed"] is False
    reserve.assert_not_called()
    client.place_order.assert_not_called()


def test_create_reserves_before_placing_and_records_broker_identity(monkeypatch):
    events = []
    reservation = OrderIntentReservation(
        intent={"idempotency_key": "protect:new12345", "status": "reserved"}, created=True
    )
    monkeypatch.setattr(
        protective_stop_manager,
        "reserve_order_intent",
        MagicMock(side_effect=lambda **kwargs: events.append("reserve") or reservation),
    )
    update = MagicMock(side_effect=lambda *args, **kwargs: events.append("update"))
    monkeypatch.setattr(protective_stop_manager, "update_order_intent", update)
    client = MagicMock()
    client.place_order.side_effect = lambda *args: events.append("place") or {
        "order_id": 88,
        "location": "/orders/88",
    }

    result = protective_stop_manager.synchronize_protective_stop(
        position=_position(), protective_intent=None, order_client=client, execute=True
    )

    assert events == ["reserve", "place", "update"]
    assert result["executed"] is True
    update.assert_called_once_with(
        result["idempotency_key"],
        status="submitted",
        broker_order_id=88,
        broker_location="/orders/88",
    )


def test_replace_uses_atomic_broker_replace_and_retires_old_intent(monkeypatch):
    new_intent = {"idempotency_key": "protect:new12345", "status": "reserved"}
    monkeypatch.setattr(
        protective_stop_manager,
        "reserve_order_intent",
        MagicMock(return_value=OrderIntentReservation(intent=new_intent, created=True)),
    )
    update = MagicMock()
    monkeypatch.setattr(protective_stop_manager, "update_order_intent", update)
    client = MagicMock()
    client.replace_order.return_value = {"order_id": 99, "location": "/orders/99"}

    result = protective_stop_manager.synchronize_protective_stop(
        position=_position(remaining_quantity=50),
        protective_intent=_intent(),
        order_client=client,
        execute=True,
    )

    client.replace_order.assert_called_once()
    client.place_order.assert_not_called()
    assert result["executed"] is True
    assert update.call_args_list == [
        call("protect:old12345", status="cancel_pending"),
        call("protect:old12345", status="canceled"),
        call(
            result["idempotency_key"],
            status="submitted",
            broker_order_id=99,
            broker_location="/orders/99",
        ),
    ]


def test_ambiguous_replace_marks_both_intents_unknown_and_does_not_retry(monkeypatch):
    new_intent = {"idempotency_key": "protect:new12345", "status": "reserved"}
    reserve = MagicMock(return_value=OrderIntentReservation(intent=new_intent, created=True))
    monkeypatch.setattr(protective_stop_manager, "reserve_order_intent", reserve)
    update = MagicMock()
    monkeypatch.setattr(protective_stop_manager, "update_order_intent", update)
    client = MagicMock()
    client.replace_order.side_effect = TimeoutError("broker response lost")

    with pytest.raises(TimeoutError):
        protective_stop_manager.synchronize_protective_stop(
            position=_position(remaining_quantity=50),
            protective_intent=_intent(),
            order_client=client,
            execute=True,
        )

    reserved_key = reserve.call_args.kwargs["idempotency_key"]
    assert update.call_args_list[-2:] == [
        call(reserved_key, status="unknown", error="broker response lost"),
        call("protect:old12345", status="unknown", error="broker response lost"),
    ]


def test_success_without_broker_order_id_remains_unknown(monkeypatch):
    reservation = OrderIntentReservation(
        intent={"idempotency_key": "protect:new12345", "status": "reserved"}, created=True
    )
    reserve = MagicMock(return_value=reservation)
    monkeypatch.setattr(protective_stop_manager, "reserve_order_intent", reserve)
    update = MagicMock()
    monkeypatch.setattr(protective_stop_manager, "update_order_intent", update)
    client = MagicMock()
    client.place_order.return_value = {"status_code": 201, "location": None, "order_id": None}

    result = protective_stop_manager.synchronize_protective_stop(
        position=_position(), protective_intent=None, order_client=client, execute=True
    )

    reserved_key = reserve.call_args.kwargs["idempotency_key"]
    assert result["status"] == "unknown"
    update.assert_called_once_with(
        reserved_key, status="unknown", error="broker_order_id_missing"
    )


def test_active_exit_prevents_stop_creation_or_replacement(monkeypatch):
    reserve = MagicMock()
    monkeypatch.setattr(protective_stop_manager, "reserve_order_intent", reserve)
    client = MagicMock()

    result = protective_stop_manager.synchronize_protective_stop(
        position=_position(),
        protective_intent=None,
        exit_intent={"status": "reserved"},
        order_client=client,
        execute=True,
    )

    assert result["decision"]["action"] == "blocked"
    assert result["decision"]["reason"] == "exit_in_progress"
    reserve.assert_not_called()
    client.place_order.assert_not_called()


def test_cancel_waits_for_authoritative_broker_confirmation(monkeypatch):
    update = MagicMock()
    monkeypatch.setattr(protective_stop_manager, "update_order_intent", update)
    client = MagicMock()
    client.cancel_order.return_value = {"status_code": 200}

    result = protective_stop_manager.synchronize_protective_stop(
        position=_position(state="closed", remaining_quantity=0),
        protective_intent=_intent(),
        order_client=client,
        execute=True,
    )

    assert result["status"] == "cancel_pending"
    update.assert_called_once_with("protect:old12345", status="cancel_pending")
    client.cancel_order.assert_called_once_with("encrypted-account", 77)
