from datetime import datetime, timezone
import os
import sys

import pandas as pd

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from services.decision_engine import DecisionConfig, classify_catalysts, decide, intraday_snapshot, market_session, news_sentiment, normalize_ohlcv


def sample_df():
    rows = []
    for i in range(30):
        close = 2.0 + i * 0.05
        rows.append({
            "o": close - 0.02,
            "h": close + 0.08,
            "l": close - 0.07,
            "c": close,
            "v": 100_000 + i * 5_000,
        })
    return pd.DataFrame(rows)


def sample_intraday_df():
    return pd.DataFrame([
        {"Open": 3.0, "High": 3.2, "Low": 2.95, "Close": 3.1, "Volume": 100_000},
        {"Open": 3.1, "High": 3.35, "Low": 3.05, "Close": 3.3, "Volume": 150_000},
        {"Open": 3.3, "High": 3.5, "Low": 3.25, "Close": 3.45, "Volume": 200_000},
    ])


def test_normalize_ohlcv_accepts_single_symbol_yahoo_multiindex():
    frame = pd.DataFrame({
        ("Open", "NVDA"): [10],
        ("High", "NVDA"): [11],
        ("Low", "NVDA"): [9],
        ("Close", "NVDA"): [10.5],
        ("Volume", "NVDA"): [1000],
    })
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)

    result = normalize_ohlcv(frame)

    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert result.iloc[0]["close"] == 10.5


def test_news_sentiment_positive():
    result = news_sentiment([{"title": "Company wins FDA approval and partnership"}])
    assert result["label"] == "positive"
    assert result["assessed"] is True
    assert result["method"] == "keyword_rules"
    assert result["score"] > 0


def test_unknown_float_keeps_strong_momentum_under_watch_not_long_candidate():
    result = decide("TEST", sample_df(), price=3.45, rvol=5, float_shares=None, macd_open=True,
                    articles=[{"title": "Company wins FDA approval and partnership"}],
                    intraday_df=sample_intraday_df(), config=DecisionConfig(min_confidence=0))
    assert "float_unknown" in result["reasons"]
    assert result["action"] != "long_candidate"


def test_news_sentiment_marks_missing_evidence_as_unassessed():
    result = news_sentiment([])

    assert result["label"] == "neutral"
    assert result["article_count"] == 0
    assert result["assessed"] is False
    assert result["method"] == "keyword_rules"


def test_classify_catalysts_fda():
    result = classify_catalysts([{"title": "Company wins FDA approval for phase 3 therapy"}])
    assert result["primary"] == "fda"
    assert result["direction"] == "positive"
    assert result["score"] > 0


def test_market_session_regular_hours():
    result = market_session(datetime(2026, 5, 29, 15, 0, tzinfo=timezone.utc))
    assert result["state"] == "open"
    assert result["is_regular"] is True


def test_intraday_snapshot_uses_intraday_vwap():
    result = intraday_snapshot(sample_df(), 3.45, intraday_df=sample_intraday_df())
    assert result["source"] == "intraday"
    assert result["vwap"] is not None
    assert result["above_vwap"] is True


def test_intraday_gap_uses_market_open_and_prior_trading_close():
    daily = pd.DataFrame([
        {"t": "2026-07-13T05:00:00Z", "o": 98, "h": 102, "l": 97, "c": 100, "v": 1_000},
        {"t": "2026-07-14T05:00:00Z", "o": 80, "h": 92, "l": 79, "c": 90, "v": 2_000},
    ])
    intraday = pd.DataFrame([
        {"t": "2026-07-14T13:30:00Z", "o": 80, "h": 84, "l": 79, "c": 83, "v": 500},
        {"t": "2026-07-14T13:35:00Z", "o": 83, "h": 91, "l": 82, "c": 90, "v": 700},
    ])

    result = intraday_snapshot(daily, 90, intraday_df=intraday)

    assert result["previous_close"] == 100
    assert result["day_open"] == 80
    assert result["gap_pct"] == -20
    assert result["halt_risk"] is False


def test_intraday_metrics_prefer_regular_session_candles():
    daily = pd.DataFrame([
        {"t": "2026-07-13T05:00:00Z", "o": 98, "h": 102, "l": 97, "c": 100, "v": 1_000},
        {"t": "2026-07-14T05:00:00Z", "o": 80, "h": 92, "l": 79, "c": 90, "v": 2_000},
    ])
    intraday = pd.DataFrame([
        {"t": "2026-07-14T12:00:00Z", "o": 140, "h": 150, "l": 135, "c": 145, "v": 100},
        {"t": "2026-07-14T13:30:00Z", "o": 80, "h": 84, "l": 79, "c": 83, "v": 500},
        {"t": "2026-07-14T13:35:00Z", "o": 83, "h": 91, "l": 82, "c": 90, "v": 700},
    ])

    result = intraday_snapshot(daily, 90, intraday_df=intraday)

    assert result["day_open"] == 80
    assert result["day_high"] == 91
    assert result["day_low"] == 79
    assert result["gap_pct"] == -20
    assert result["halt_risk"] is False


def test_decide_long_candidate():
    decision = decide(
        "TEST",
        sample_df(),
        price=3.45,
        rvol=4.2,
        float_shares=12_000_000,
        macd_open=True,
        articles=[{"title": "TEST gets FDA approval and raises guidance"}],
        intraday_df=sample_intraday_df(),
        config=DecisionConfig(min_rvol=3.0, max_float=20_000_000),
    )
    assert decision["action"] == "long_candidate"
    assert decision["catalyst"]["primary"] == "fda"
    assert decision["intraday"]["source"] == "intraday"
    assert decision["confidence"] >= decision["guardrails"]["min_confidence"]
    assert decision["trade_plan"]["stop_loss"] < decision["trade_plan"]["entry_reference"]
    assert decision["trade_plan"]["take_profit"] > decision["trade_plan"]["entry_reference"]


def test_decide_uses_current_quote_and_caps_stale_resistance():
    frame = pd.DataFrame({
        "o": [290, 285, 98],
        "h": [310, 300, 105],
        "l": [280, 275, 95],
        "c": [295, 290, 100],
        "v": [1_000, 1_000, 5_000],
    })

    decision = decide("TEST", frame, price=98, rvol=4, macd_open=True)

    assert decision["technicals"]["last"] == 98
    assert decision["trade_plan"]["entry_reference"] == 98
    assert decision["trade_plan"]["take_profit"] <= 98 * 1.25


def test_decide_avoids_negative_news():
    decision = decide(
        "TEST",
        sample_df(),
        price=3.45,
        rvol=4.2,
        float_shares=12_000_000,
        macd_open=True,
        articles=[{"title": "TEST faces SEC investigation and offering warning"}],
    )
    assert decision["action"] == "avoid"
    assert "negative_news_terms" in decision["reasons"]


def test_decide_blocks_offering_catalyst():
    decision = decide(
        "TEST",
        sample_df(),
        price=3.45,
        rvol=4.2,
        float_shares=12_000_000,
        macd_open=True,
        articles=[{"title": "TEST announces registered direct offering with warrants"}],
    )
    assert decision["action"] == "avoid"
    assert decision["catalyst"]["primary"] == "offering_dilution"
