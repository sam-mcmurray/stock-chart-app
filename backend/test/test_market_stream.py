from services.market_stream import (
    normalize_chart_equity,
    normalize_equity_book,
    normalize_levelone_equity,
    merge_levelone_equity,
    response_errors,
)


def test_levelone_normalizer_converts_documented_lots_to_shares():
    result = normalize_levelone_equity({
        "key": "F",
        "delayed": False,
        "assetMainType": "EQUITY",
        "1": 13.99,
        "2": 14.0,
        "3": 14.0,
        "4": 65,
        "5": 194,
        "8": 21_120_078,
        "32": "Normal",
        "33": 13.995,
        "34": 1_800_000_000_000,
    }, received_at_ms=1_800_000_000_100)

    assert result["bid_size_lots"] == 65
    assert result["bid_size_shares"] == 6500
    assert result["ask_size_shares"] == 19400
    assert result["quote_time_ms"] == 1_800_000_000_000
    assert result["delayed"] is False


def test_chart_normalizer_uses_documented_minute_fields():
    result = normalize_chart_equity({
        "key": "F", "1": 7, "2": 14, "3": 14.1, "4": 13.9,
        "5": 14.05, "6": 12345, "7": 1_800_000_000_000, "8": 20260911,
    }, received_at_ms=1_800_000_000_100)

    assert result["close"] == 14.05
    assert result["volume"] == 12345
    assert result["chart_time_ms"] == 1_800_000_000_000


def test_sparse_levelone_change_preserves_prior_quote_fields():
    previous = normalize_levelone_equity({
        "key": "F", "1": 13.99, "2": 14, "4": 65, "5": 194, "32": "Normal",
    }, received_at_ms=100)
    sparse = normalize_levelone_equity(
        {"key": "F", "8": 22_000_000}, received_at_ms=200
    )

    merged = merge_levelone_equity(previous, sparse)

    assert merged["bid"] == 13.99
    assert merged["ask_size_shares"] == 19400
    assert merged["security_status"] == "Normal"
    assert merged["total_volume"] == 22_000_000
    assert merged["received_at_ms"] == 200


def test_book_normalizer_preserves_whole_levels_and_market_makers():
    result = normalize_equity_book({
        "key": "F",
        "1": 1_800_000_000_000,
        "2": [{"0": 13.99, "1": 6500, "2": 1, "3": [{"0": "XNAS", "1": 6500, "2": 1_800_000_000_000}]}],
        "3": [[14.0, 19400, 2, []]],
    }, service="NASDAQ_BOOK", received_at_ms=1_800_000_000_100)

    assert result["bids"][0]["price"] == 13.99
    assert result["bids"][0]["market_makers"][0]["id"] == "XNAS"
    assert result["asks"][0]["size"] == 19400


def test_nonzero_subscription_ack_is_reported():
    assert response_errors([
        {"service": "NASDAQ_BOOK", "command": "SUBS", "content": {"code": 9, "msg": "denied"}},
        {"service": "CHART_EQUITY", "command": "SUBS", "content": {"code": 0, "msg": "ok"}},
    ]) == [{
        "service": "NASDAQ_BOOK",
        "command": "SUBS",
        "code": 9,
        "message": "denied",
    }]
