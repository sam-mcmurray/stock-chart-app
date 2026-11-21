import pytest

from services.protective_stop import (
    build_protective_stop_order,
    evaluate_protective_stop,
    normalize_equity_stop_price,
    protection_idempotency_key,
)


def _position(**updates):
    return {
        "state": "open",
        "symbol": "ABCD",
        "remaining_quantity": 100,
        "average_entry_price": 10,
        "hard_stop_price": 9.47,
        "active_stop_price": 9.47,
        **updates,
    }


def test_build_protective_stop_uses_documented_gtc_stop_sell():
    payload = build_protective_stop_order(symbol="abcd", quantity=100, stop_price=9.479)

    assert payload == {
        "orderType": "STOP",
        "session": "NORMAL",
        "duration": "GOOD_TILL_CANCEL",
        "stopPrice": 9.47,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": "SELL",
            "quantity": 100,
            "instrument": {"symbol": "ABCD", "assetType": "EQUITY"},
        }],
    }


def test_sub_dollar_stop_uses_four_decimal_precision_and_rounds_down():
    assert normalize_equity_stop_price(0.98769) == 0.9876


def test_missing_protection_is_created_only_for_confirmed_filled_quantity():
    decision = evaluate_protective_stop(
        _position(state="entry_pending", remaining_quantity=25, average_entry_price=10.05),
        None,
    )

    assert decision.action == "create"
    assert decision.quantity == 25


def test_unfilled_entry_is_not_given_an_invented_stop_order():
    decision = evaluate_protective_stop(
        _position(state="entry_pending", remaining_quantity=0, average_entry_price=None),
        None,
    )

    assert decision.action == "none"


def test_existing_stop_is_replaced_after_partial_exit():
    intent = {
        "status": "submitted",
        "quantity": 100,
        "order_payload": {"stopPrice": 9.47},
    }

    decision = evaluate_protective_stop(_position(state="partial_taken", remaining_quantity=50), intent)

    assert decision.action == "replace"
    assert decision.quantity == 50


def test_current_stop_requires_no_broker_call():
    intent = {
        "status": "submitted",
        "quantity": 100,
        "order_payload": {"stopPrice": 9.47},
    }

    assert evaluate_protective_stop(_position(), intent).action == "none"


def test_closed_position_cancels_still_active_stop():
    decision = evaluate_protective_stop(
        _position(state="closed", remaining_quantity=0),
        {"status": "submitted", "quantity": 100, "order_payload": {"stopPrice": 9.47}},
    )

    assert decision.action == "cancel"


def test_break_even_stop_is_valid_after_position_moves_up():
    decision = evaluate_protective_stop(_position(active_stop_price=10), None)

    assert decision.action == "create"
    assert decision.stop_price == 10


def test_stop_below_original_hard_stop_fails_closed():
    decision = evaluate_protective_stop(_position(active_stop_price=9.40), None)

    assert decision.action == "blocked"
    assert decision.reason == "invalid_long_stop"


def test_invalid_order_builder_inputs_are_rejected():
    with pytest.raises(ValueError):
        build_protective_stop_order(symbol="", quantity=1, stop_price=1)
    with pytest.raises(ValueError):
        build_protective_stop_order(symbol="AAPL", quantity=0, stop_price=1)


def test_protection_key_is_stable_for_retry_but_changes_for_later_revision():
    initial = protection_idempotency_key(position_id="pos:1", quantity=100, stop_price=9.479)

    assert initial == protection_idempotency_key(
        position_id="pos:1", quantity=100, stop_price=9.479
    )
    assert initial != protection_idempotency_key(
        position_id="pos:1",
        quantity=100,
        stop_price=9.479,
        replaces_idempotency_key=initial,
    )


def test_unknown_protection_state_fails_closed_without_broker_action():
    decision = evaluate_protective_stop(
        _position(),
        {"status": "unknown", "quantity": 100, "order_payload": {"stopPrice": 9.47}},
    )

    assert decision.action == "blocked"
    assert decision.reason == "protection_state_unknown"
