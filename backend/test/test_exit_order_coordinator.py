from unittest.mock import MagicMock, call

import pytest

from services import exit_order_coordinator as coordinator
from services.trade_journal import OrderIntentReservation


def _position(**updates):
    return {
        "position_id": "pos:abc",
        "account_id": "encrypted-account",
        "symbol": "ABCD",
        "remaining_quantity": 100,
        **updates,
    }


def _decision(**updates):
    return {
        "action": "exit_all",
        "quantity": 100,
        "reason": "hard_stop",
        **updates,
    }


def _protective(**updates):
    return {
        "idempotency_key": "protect:old12345",
        "status": "submitted",
        "broker_order_id": 77,
        **updates,
    }


def _reserved_exit(**updates):
    return {
        "idempotency_key": coordinator.exit_idempotency_key(
            position_id="pos:abc", quantity=100
        ),
        "status": "reserved",
        **updates,
    }


def test_partial_exit_is_blocked_when_a_standing_broker_sell_exists():
    plan = coordinator.evaluate_exit_coordination(
        position=_position(),
        exit_decision=_decision(action="take_partial", quantity=50),
        protective_intent=_protective(),
        exit_intent=None,
    )

    assert plan.action == "blocked"
    assert plan.reason == "standing_broker_order_conflicts_with_local_partial"


def test_local_partial_exit_is_reserved_then_submitted_only_when_triggered(monkeypatch):
    key = coordinator.exit_idempotency_key(
        position_id="pos:abc", quantity=50, action="take_partial"
    )
    reservation = OrderIntentReservation(
        intent={"idempotency_key": key, "status": "reserved"}, created=True
    )
    reserve = MagicMock(return_value=reservation)
    monkeypatch.setattr(coordinator, "reserve_order_intent", reserve)
    client = MagicMock()

    first = coordinator.synchronize_exit(
        position=_position(),
        exit_decision=_decision(action="take_partial", quantity=50),
        protective_intent=None,
        exit_intent=None,
        order_client=client,
        execute=True,
    )

    assert first["decision"]["action"] == "reserve_exit"
    assert reserve.call_args.kwargs["quantity"] == 50
    client.place_order.assert_not_called()

    monkeypatch.setattr(
        coordinator,
        "claim_order_intent_submission",
        MagicMock(return_value={"idempotency_key": key, "status": "submitting"}),
    )
    update = MagicMock()
    monkeypatch.setattr(coordinator, "update_order_intent", update)
    client.place_order.return_value = {"order_id": 91, "location": "/orders/91"}
    second = coordinator.synchronize_exit(
        position=_position(),
        exit_decision=_decision(action="take_partial", quantity=50),
        protective_intent=None,
        exit_intent={"idempotency_key": key, "status": "reserved"},
        order_client=client,
        execute=True,
    )

    assert second["decision"]["action"] == "submit_exit"
    assert second["status"] == "submitted"
    assert second["order_payload"]["orderLegCollection"][0]["quantity"] == 50


def test_first_full_exit_pass_reserves_before_any_broker_call(monkeypatch):
    key = coordinator.exit_idempotency_key(position_id="pos:abc", quantity=100)
    reservation = OrderIntentReservation(
        intent={"idempotency_key": key, "status": "reserved"}, created=True
    )
    reserve = MagicMock(return_value=reservation)
    monkeypatch.setattr(coordinator, "reserve_order_intent", reserve)
    client = MagicMock()

    result = coordinator.synchronize_full_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=_protective(),
        exit_intent=None,
        order_client=client,
        execute=True,
    )

    assert result["decision"]["action"] == "reserve_exit"
    assert result["reserved"] is True
    reserve.assert_called_once()
    client.cancel_order.assert_not_called()
    client.place_order.assert_not_called()


def test_reserved_exit_cancels_protection_but_waits_for_confirmation(monkeypatch):
    update = MagicMock()
    monkeypatch.setattr(coordinator, "update_order_intent", update)
    client = MagicMock()
    client.cancel_order.return_value = {"status_code": 200}

    result = coordinator.synchronize_full_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=_protective(),
        exit_intent=_reserved_exit(),
        order_client=client,
        execute=True,
    )

    assert result["status"] == "cancel_pending"
    update.assert_called_once_with("protect:old12345", status="cancel_pending")
    client.cancel_order.assert_called_once_with("encrypted-account", 77)
    client.place_order.assert_not_called()


def test_cancel_timeout_marks_protection_unknown_and_does_not_submit(monkeypatch):
    update = MagicMock()
    monkeypatch.setattr(coordinator, "update_order_intent", update)
    client = MagicMock()
    client.cancel_order.side_effect = TimeoutError("cancel response lost")

    with pytest.raises(TimeoutError):
        coordinator.synchronize_full_exit(
            position=_position(),
            exit_decision=_decision(),
            protective_intent=_protective(),
            exit_intent=_reserved_exit(),
            order_client=client,
            execute=True,
        )

    assert update.call_args_list == [
        call("protect:old12345", status="cancel_pending"),
        call("protect:old12345", status="unknown", error="cancel response lost"),
    ]
    client.place_order.assert_not_called()


def test_confirmed_canceled_protection_submits_only_after_atomic_claim(monkeypatch):
    exit_intent = _reserved_exit()
    claim = MagicMock(return_value={**exit_intent, "status": "submitting"})
    update = MagicMock()
    monkeypatch.setattr(coordinator, "claim_order_intent_submission", claim)
    monkeypatch.setattr(coordinator, "update_order_intent", update)
    client = MagicMock()
    client.place_order.return_value = {"order_id": 88, "location": "/orders/88"}

    result = coordinator.synchronize_full_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=_protective(status="canceled"),
        exit_intent=exit_intent,
        order_client=client,
        execute=True,
    )

    assert result["status"] == "submitted"
    claim.assert_called_once_with(exit_intent["idempotency_key"])
    client.place_order.assert_called_once()
    update.assert_called_once_with(
        exit_intent["idempotency_key"],
        status="submitted",
        broker_order_id=88,
        broker_location="/orders/88",
    )


def test_failed_atomic_claim_never_calls_broker(monkeypatch):
    monkeypatch.setattr(
        coordinator, "claim_order_intent_submission", MagicMock(return_value=None)
    )
    client = MagicMock()

    result = coordinator.synchronize_full_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=_protective(status="canceled"),
        exit_intent=_reserved_exit(),
        order_client=client,
        execute=True,
    )

    assert result["replayed"] is True
    client.place_order.assert_not_called()


def test_submit_timeout_marks_exit_unknown_and_never_blindly_retries(monkeypatch):
    exit_intent = _reserved_exit()
    monkeypatch.setattr(
        coordinator,
        "claim_order_intent_submission",
        MagicMock(return_value={**exit_intent, "status": "submitting"}),
    )
    update = MagicMock()
    monkeypatch.setattr(coordinator, "update_order_intent", update)
    client = MagicMock()
    client.place_order.side_effect = TimeoutError("submit response lost")

    with pytest.raises(TimeoutError):
        coordinator.synchronize_full_exit(
            position=_position(),
            exit_decision=_decision(),
            protective_intent=None,
            exit_intent=exit_intent,
            order_client=client,
            execute=True,
        )

    update.assert_called_once_with(
        exit_intent["idempotency_key"], status="unknown", error="submit response lost"
    )
    retry_plan = coordinator.evaluate_exit_coordination(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=None,
        exit_intent={**exit_intent, "status": "unknown"},
    )
    assert retry_plan.action == "wait"


@pytest.mark.parametrize("status", ["submitting", "unknown", "partially_filled"])
def test_restart_with_ambiguous_or_active_exit_never_resubmits(status):
    client = MagicMock()

    result = coordinator.synchronize_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=None,
        exit_intent={**_reserved_exit(), "status": status},
        order_client=client,
        execute=True,
    )

    assert result["decision"]["action"] == "wait"
    client.place_order.assert_not_called()


def test_restart_with_durable_reservation_can_claim_and_submit_once(monkeypatch):
    exit_intent = _reserved_exit()
    claim = MagicMock(
        side_effect=[{**exit_intent, "status": "submitting"}, None]
    )
    monkeypatch.setattr(coordinator, "claim_order_intent_submission", claim)
    monkeypatch.setattr(coordinator, "update_order_intent", MagicMock())
    client = MagicMock()
    client.place_order.return_value = {"order_id": 88, "location": "/orders/88"}

    first = coordinator.synchronize_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=None,
        exit_intent=exit_intent,
        order_client=client,
        execute=True,
    )
    replay = coordinator.synchronize_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=None,
        exit_intent=exit_intent,
        order_client=client,
        execute=True,
    )

    assert first["status"] == "submitted"
    assert replay["replayed"] is True
    client.place_order.assert_called_once()


def test_dry_run_plans_without_mutating_journal_or_broker(monkeypatch):
    reserve = MagicMock()
    monkeypatch.setattr(coordinator, "reserve_order_intent", reserve)
    client = MagicMock()

    result = coordinator.synchronize_full_exit(
        position=_position(),
        exit_decision=_decision(),
        protective_intent=_protective(),
        exit_intent=None,
        order_client=client,
        execute=False,
    )

    assert result["decision"]["action"] == "reserve_exit"
    assert result["executed"] is False
    reserve.assert_not_called()
    client.cancel_order.assert_not_called()
