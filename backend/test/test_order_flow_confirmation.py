import json

import pytest

from services.order_flow_confirmation import (
    OrderFlowLimits,
    evaluate_order_flow_confirmation,
)


def _snapshot(**updates):
    metrics = {
        "service": "NYSE_BOOK",
        "market_age_seconds": 0.2,
        "receive_age_seconds": 0.1,
        "best_bid": 10,
        "best_ask": 10.01,
        "bid_depth": 800,
        "ask_depth": 400,
        "imbalance": 0.3333,
        **updates,
    }
    return {"book_metrics": metrics}


def test_fresh_balanced_book_confirms_but_does_not_propose_trade():
    result = evaluate_order_flow_confirmation(_snapshot())

    assert result.allowed is True
    assert result.reasons == []
    assert not hasattr(result, "action")


def test_missing_or_stale_book_fails_closed():
    result = evaluate_order_flow_confirmation({"book_metrics": {}})

    assert result.allowed is False
    assert set(result.reasons) >= {
        "level2_missing",
        "level2_market_stale",
        "level2_receive_stale",
        "level2_invalid_top",
        "level2_depth_too_low",
        "level2_ask_imbalance",
    }


def test_severe_ask_imbalance_vetoes_entry():
    result = evaluate_order_flow_confirmation(
        _snapshot(bid_depth=200, ask_depth=1800, imbalance=-0.8)
    )

    assert result.allowed is False
    assert result.reasons == ["level2_ask_imbalance"]


def test_order_flow_thresholds_are_configurable():
    result = evaluate_order_flow_confirmation(
        _snapshot(bid_depth=200, ask_depth=800, imbalance=-0.6),
        limits=OrderFlowLimits(min_imbalance=-0.7),
    )

    assert result.allowed is True


@pytest.mark.parametrize("field", [
    "market_age_seconds", "receive_age_seconds", "best_bid", "best_ask",
    "bid_depth", "ask_depth", "imbalance",
])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "bad", True])
def test_invalid_book_numbers_block_without_crashing(field, value):
    result = evaluate_order_flow_confirmation(_snapshot(**{field: value}))
    assert result.allowed is False
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.parametrize("field,value,reason", [
    ("market_age_seconds", -0.01, "level2_market_stale"),
    ("receive_age_seconds", -1, "level2_receive_stale"),
    ("best_bid", -1, "level2_invalid_top"),
    ("best_ask", 0, "level2_invalid_top"),
    ("bid_depth", -1, "level2_invalid_depth"),
    ("ask_depth", 1.5, "level2_invalid_depth"),
    ("imbalance", 1.1, "level2_ask_imbalance"),
    ("imbalance", -1.1, "level2_ask_imbalance"),
])
def test_out_of_domain_book_values_block(field, value, reason):
    result = evaluate_order_flow_confirmation(_snapshot(**{field: value}))
    assert not result.allowed
    assert reason in result.reasons
