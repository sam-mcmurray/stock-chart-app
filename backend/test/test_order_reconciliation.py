from datetime import datetime, timezone

import pytest

from services.order_reconciliation import (
    ambiguous_order_query_window,
    match_ambiguous_order,
    normalize_schwab_order,
)


def test_normalize_schwab_order_preserves_partial_fills_and_weighted_price():
    order = {
        "orderId": 99,
        "status": "PARTIALLY_FILLED",
        "quantity": 100,
        "filledQuantity": 40,
        "remainingQuantity": 60,
        "orderLegCollection": [{
            "legId": 1,
            "instruction": "BUY",
            "quantity": 100,
            "instrument": {"symbol": "ABCD", "assetType": "EQUITY"},
        }],
        "orderActivityCollection": [
            {
                "activityType": "EXECUTION",
                "executionType": "FILL",
                "executionLegs": [{"legId": 1, "quantity": 10, "price": 5.0, "time": "2026-01-01T15:00:00Z"}],
            },
            {
                "activityType": "EXECUTION",
                "executionType": "FILL",
                "executionLegs": [{"legId": 1, "quantity": 30, "price": 5.2, "time": "2026-01-01T15:00:01Z"}],
            },
        ],
    }

    result = normalize_schwab_order(order)

    assert result["status"] == "partially_filled"
    assert result["filled_quantity"] == 40
    assert result["remaining_quantity"] == 60
    assert result["average_fill_price"] == pytest.approx(5.15)
    assert [fill["quantity"] for fill in result["fills"]] == [10, 30]
    assert all(fill["symbol"] == "ABCD" for fill in result["fills"])


def test_normalize_schwab_order_deduplicates_repeated_execution_legs():
    fill = {"legId": 0, "quantity": 5, "price": 10, "time": "2026-01-01T15:00:00Z"}
    result = normalize_schwab_order({
        "orderId": 1,
        "status": "FILLED",
        "orderLegCollection": [{
            "legId": 0,
            "instruction": "SELL",
            "instrument": {"symbol": "XYZ", "assetType": "EQUITY"},
        }],
        "orderActivityCollection": [
            {"activityType": "EXECUTION", "executionType": "FILL", "executionLegs": [fill]},
            {"activityType": "EXECUTION", "executionType": "FILL", "executionLegs": [fill]},
        ],
    })

    assert result["status"] == "filled"
    assert len(result["fills"]) == 1
    assert result["filled_quantity"] == 5


def test_normalize_schwab_order_does_not_invent_missing_fill_details():
    result = normalize_schwab_order({
        "orderId": 2,
        "status": "WORKING",
        "quantity": 10,
        "filledQuantity": 0,
        "remainingQuantity": 10,
        "orderLegCollection": [],
    })

    assert result["status"] == "submitted"
    assert result["fills"] == []
    assert result["average_fill_price"] == 0


def test_normalize_schwab_order_rejects_missing_order_id():
    with pytest.raises(ValueError, match="orderId"):
        normalize_schwab_order({"status": "FILLED"})


def _ambiguous_intent():
    return {
        "idempotency_key": "exit:abcdefgh",
        "symbol": "ABCD",
        "side": "SELL",
        "quantity": 100,
        "order_type": "MARKET",
        "created_at": datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, 15, 0, 20, tzinfo=timezone.utc),
    }


def _broker_order(order_id=77, **updates):
    result = {
        "orderId": order_id,
        "status": "FILLED",
        "orderType": "MARKET",
        "enteredTime": "2026-01-01T15:00:05Z",
        "orderLegCollection": [{
            "legId": 1,
            "instruction": "SELL",
            "quantity": 100,
            "instrument": {"symbol": "ABCD", "assetType": "EQUITY"},
        }],
    }
    result.update(updates)
    return result


def test_ambiguous_order_window_is_narrow_and_utc():
    start, end = ambiguous_order_query_window(_ambiguous_intent())

    assert start == "2026-01-01T14:58:00.000000Z"
    assert end == "2026-01-01T15:02:20.000000Z"


def test_ambiguous_order_match_requires_one_exact_candidate():
    intent = _ambiguous_intent()

    assert match_ambiguous_order(intent, [_broker_order()])["orderId"] == 77
    assert match_ambiguous_order(
        intent, [_broker_order(), _broker_order(order_id=78)]
    ) is None
    assert match_ambiguous_order(
        intent, [_broker_order(orderLegCollection=[{
            "legId": 1,
            "instruction": "SELL",
            "quantity": 99,
            "instrument": {"symbol": "ABCD"},
        }])]
    ) is None
    assert match_ambiguous_order(
        intent, [_broker_order(enteredTime="2026-01-01T16:00:00Z")]
    ) is None
