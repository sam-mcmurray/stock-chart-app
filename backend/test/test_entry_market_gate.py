from datetime import datetime, timezone
import pytest

from services.entry_market_gate import (
    EntryMarketLimits,
    evaluate_entry_market_gate,
    normalize_schwab_equity_hours,
    normalize_schwab_equity_quote,
)


NOW_MS = 1_800_000_000_000


@pytest.mark.parametrize("volume,valid", [(0, True), (3500, True), (None, False),
    (-1, False), (float("nan"), False), (float("inf"), False), ("bad", False), (True, False)])
def test_quote_volume_validity_distinguishes_missing_from_zero(volume, valid):
    quote = normalize_schwab_equity_quote(
        {"TEST": {"quote": {"totalVolume": volume}}}, symbol="TEST", fetched_at_ms=NOW_MS)
    assert quote["total_volume_valid"] is valid


def _quote(**updates):
    return {
        "realtime": True,
        "asset_main_type": "EQUITY",
        "bid": 9.99,
        "ask": 10.01,
        "bid_size": 500,
        "ask_size": 600,
        "last": 10,
        "mark": 10,
        "quote_time_ms": NOW_MS - 1000,
        "security_status": "Normal",
        "total_volume": 500_000,
        **updates,
    }


def test_normalize_documented_schwab_equity_quote_fields():
    result = normalize_schwab_equity_quote({
        "ABCD": {
            "assetMainType": "EQUITY",
            "realtime": True,
            "quote": {
                "bidPrice": 9.99,
                "askPrice": 10.01,
                "bidSize": 500,
                "askSize": 600,
                "lastPrice": 10,
                "mark": 10,
                "quoteTime": NOW_MS - 1000,
                "tradeTime": NOW_MS - 2000,
                "securityStatus": "Normal",
                "totalVolume": 500_000,
            },
        }
    }, symbol="abcd", fetched_at_ms=NOW_MS)

    assert result["symbol"] == "ABCD"
    assert result["bid_size"] == 500
    assert result["security_status"] == "Normal"
    assert result["realtime"] is True


def test_market_hours_requires_current_time_inside_documented_regular_interval():
    now = datetime(2027, 1, 4, 15, 0, tzinfo=timezone.utc)
    payload = {"equity": {"EQ": {
        "date": "2027-01-04",
        "isOpen": True,
        "sessionHours": {"regularMarket": [{
            "start": "2027-01-04T09:30:00-05:00",
            "end": "2027-01-04T16:00:00-05:00",
        }]},
    }}}

    assert normalize_schwab_equity_hours(payload, now=now)["is_regular"] is True


def test_healthy_realtime_market_allows_entry():
    result = evaluate_entry_market_gate(
        quote=_quote(),
        market_hours={"market_open": True, "is_regular": True, "session": "regular"},
        now_ms=NOW_MS,
    )

    assert result.allowed is True
    assert result.spread_bps == 20


def test_stale_delayed_halted_wide_or_illiquid_quote_fails_closed():
    result = evaluate_entry_market_gate(
        quote=_quote(
            realtime=False,
            quote_time_ms=NOW_MS - 10_000,
            ask=10.20,
            bid_size=0,
            ask_size=0,
            total_volume=10,
            security_status="Halted",
        ),
        market_hours={"market_open": False, "is_regular": False, "session": "closed"},
        now_ms=NOW_MS,
        heuristic_halt_risk=True,
    )

    assert result.allowed is False
    assert set(result.reasons) >= {
        "quote_not_realtime",
        "quote_stale",
        "spread_too_wide",
        "bid_liquidity_too_low",
        "ask_liquidity_too_low",
        "day_volume_too_low",
        "security_not_normal",
        "halt_risk",
        "market_not_regular",
    }


def test_price_range_is_configurable():
    result = evaluate_entry_market_gate(
        quote=_quote(mark=55, bid=54.99, ask=55.01),
        market_hours={"market_open": True, "is_regular": True, "session": "regular"},
        now_ms=NOW_MS,
        limits=EntryMarketLimits(max_price=50),
    )

    assert result.reasons == ["price_out_of_range"]


def test_small_share_count_is_allowed_when_book_dollar_liquidity_is_sufficient():
    result = evaluate_entry_market_gate(
        quote=_quote(bid=334.50, ask=334.53, bid_size=80, ask_size=20, mark=334.51),
        market_hours={"market_open": True, "is_regular": True, "session": "regular"},
        now_ms=NOW_MS,
        limits=EntryMarketLimits(max_price=500),
    )

    assert result.allowed is True


def test_small_low_priced_book_fails_dollar_liquidity_gate():
    result = evaluate_entry_market_gate(
        quote=_quote(bid=1.99, ask=2.01, bid_size=100, ask_size=100, mark=2),
        market_hours={"market_open": True, "is_regular": True, "session": "regular"},
        now_ms=NOW_MS,
    )

    assert "bid_notional_too_low" in result.reasons
    assert "ask_notional_too_low" in result.reasons
