import os
import sys

import pandas as pd

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from services.market_signal import market_signal
from services.semantic_signal import semantic_signal


def test_semantic_signal_extracts_structured_news_features():
    signal = semantic_signal(
        "NVDA",
        articles=[
            {
                "title": "Company raises guidance after earnings beat",
                "summary": "Analysts cite strong demand.",
            }
        ],
        rag_signal={"confidence": 0.72, "hit_count": 2, "graph_count": 1},
    )

    assert signal["ticker"] == "NVDA"
    assert signal["sentiment"] == "positive"
    assert signal["event_type"] == "earnings"
    assert signal["confidence"] == 0.72
    assert signal["risk_flags"] == []


def test_market_signal_extracts_numeric_features():
    closes = list(range(10, 35))
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [price + 1 for price in closes],
            "Low": [price - 1 for price in closes],
            "Close": closes,
            "Volume": [1_500_000] * len(closes),
        }
    )

    signal = market_signal("AAPL", df, price=34, rvol=3.2, macd_open=True)

    assert signal["ticker"] == "AAPL"
    assert signal["price"] == 34
    assert signal["rvol"] == 3.2
    assert signal["large_rvol"] is True
    assert signal["trend_score"] > 0
    assert signal["liquidity_score"] == 1.0
    assert signal["technicals"]["macd_open"] is True


def test_market_signal_flags_below_3x_rvol():
    closes = list(range(10, 35))
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [price + 1 for price in closes],
            "Low": [price - 1 for price in closes],
            "Close": closes,
            "Volume": [1_500_000] * len(closes),
        }
    )

    signal = market_signal("AAPL", df, price=34, rvol=2.9, macd_open=True)

    assert signal["large_rvol"] is False
    assert "rvol_below_3x" in signal["risk_flags"]
