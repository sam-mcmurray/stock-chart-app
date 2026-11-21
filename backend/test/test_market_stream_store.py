import json
from unittest.mock import MagicMock

from services.market_stream_store import book_metrics, load_market_stream_snapshot, select_listing_book


def test_listing_exchange_selects_one_book_without_double_counting():
    nasdaq = {"service": "NASDAQ_BOOK", "market_time_ms": 200}
    nyse = {"service": "NYSE_BOOK", "market_time_ms": 300}

    assert select_listing_book({"exchange_id": "Q"}, nasdaq, nyse) is nasdaq
    assert select_listing_book({"exchange_id": "N"}, nasdaq, nyse) is nyse
    assert select_listing_book({}, nasdaq, nyse) is nyse


def test_book_metrics_calculates_bounded_depth_and_imbalance():
    result = book_metrics({
        "service": "NYSE_BOOK",
        "market_time_ms": 9_900,
        "received_at_ms": 9_950,
        "bids": [{"price": 10, "size": 600}, {"price": 9.99, "size": 200}],
        "asks": [{"price": 10.01, "size": 200}, {"price": 10.02, "size": 200}],
    }, now_ms=10_000, levels=2)

    assert result["bid_depth"] == 800
    assert result["ask_depth"] == 400
    assert result["imbalance"] == 0.3333
    assert result["market_age_seconds"] == 0.1


def test_load_snapshot_sorts_and_bounds_chart_candles():
    redis_client = MagicMock()
    redis_client.hget.side_effect = [
        json.dumps({"symbol": "F", "exchange_id": "N", "received_at_ms": 9_900, "quote_time_ms": 9_800}),
        json.dumps({"service": "NASDAQ_BOOK", "market_time_ms": 9_700, "bids": [], "asks": []}),
        json.dumps({"service": "NYSE_BOOK", "market_time_ms": 9_900, "bids": [], "asks": []}),
    ]
    redis_client.hgetall.return_value = {
        "300": json.dumps({"chart_time_ms": 300}),
        "100": json.dumps({"chart_time_ms": 100}),
        "200": json.dumps({"chart_time_ms": 200}),
    }

    result = load_market_stream_snapshot(
        redis_client,
        symbol="f",
        levelone_key="levelone",
        nasdaq_book_key="nasdaq",
        nyse_book_key="nyse",
        chart_key_prefix="chart:",
        chart_limit=2,
        now_ms=10_000,
    )

    assert result["book"]["service"] == "NYSE_BOOK"
    assert [row["chart_time_ms"] for row in result["candles"]] == [200, 300]
    assert result["quote_age_seconds"] == 0.2
