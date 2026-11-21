from services.position_exit_controller import (
    ExitStreamLimits,
    candle_momentum_reversal,
    evaluate_position_exit,
)


def _position(**updates):
    return {
        "remaining_quantity": 100,
        "average_entry_price": 10,
        "hard_stop_price": 9,
        "active_stop_price": 9,
        "peak_price": 10,
        "partial_taken": False,
        **updates,
    }


def _candles(reversal=False):
    if reversal:
        return [
            {"open": 11.5, "high": 11.6, "low": 11.3, "close": 11.4},
            {"open": 11.4, "high": 11.5, "low": 11.1, "close": 11.2},
            {"open": 11.2, "high": 11.3, "low": 10.9, "close": 11.0},
        ]
    return [
        {"open": 10.8, "high": 11.0, "low": 10.7, "close": 10.9},
        {"open": 10.9, "high": 11.2, "low": 10.8, "close": 11.1},
        {"open": 11.1, "high": 11.4, "low": 11.0, "close": 11.3},
    ]


def _snapshot(*, price=11.3, imbalance=0.1, reversal=False, quote_age=0.2, book_age=0.2):
    return {
        "levelone": {
            "last": price,
            "mark": price,
            "security_status": "Normal",
            "delayed": False,
        },
        "quote_age_seconds": quote_age,
        "levelone_receive_age_seconds": 0.1,
        "book_metrics": {
            "service": "NASDAQ_BOOK",
            "market_age_seconds": book_age,
            "receive_age_seconds": 0.1,
            "imbalance": imbalance,
        },
        "candles": _candles(reversal),
    }


def test_candle_reversal_requires_price_confirmation():
    candles = _candles(True)

    assert candle_momentum_reversal(candles, current_price=11.0) is True
    assert candle_momentum_reversal(candles, current_price=11.1) is False


def test_adverse_level2_cannot_exit_without_candle_reversal():
    result = evaluate_position_exit(
        _position(partial_taken=True),
        _snapshot(price=10.5, imbalance=-0.8, reversal=False),
    )

    assert result.action == "hold"
    assert result.order_flow_fresh is True


def test_confirmed_reversal_requires_fresh_adverse_level2():
    confirmed = evaluate_position_exit(
        _position(partial_taken=True),
        _snapshot(price=11.0, imbalance=-0.5, reversal=True),
    )
    stale_book = evaluate_position_exit(
        _position(partial_taken=True),
        _snapshot(price=11.0, imbalance=-0.5, reversal=True, book_age=9),
    )

    assert confirmed.action == "exit_all"
    assert confirmed.reason == "confirmed_momentum_reversal"
    assert stale_book.action == "hold"
    assert stale_book.order_flow_imbalance is None


def test_stale_quote_requests_risk_reducing_exit():
    result = evaluate_position_exit(
        _position(),
        _snapshot(quote_age=10),
        limits=ExitStreamLimits(max_quote_age_seconds=3),
    )

    assert result.action == "exit_all"
    assert result.reason == "market_data_stale"


def test_controller_ratchets_stop_and_peak_without_regression():
    result = evaluate_position_exit(
        _position(active_stop_price=10.5, peak_price=11.8, partial_taken=True),
        _snapshot(price=11.4),
    )

    assert result.active_stop >= 10.5
    assert result.peak_price == 11.8
