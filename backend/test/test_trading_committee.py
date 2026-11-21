import pandas as pd
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from drl_agent.committee import TradingCommittee


def _df(closes):
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [price * 1.01 for price in closes],
            "Low": [price * 0.99 for price in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        }
    )


def test_committee_approves_constructive_setup():
    committee = TradingCommittee()

    report = committee.deliberate(
        _df([10, 10.2, 10.4, 10.8, 11.2, 11.8, 12.3, 12.9, 13.4, 13.9]),
        {
            "ppo_action": 0,
            "rag_score": 7.0,
            "rag_confidence": 0.8,
            "min_rag_confidence": 0.2,
            "current_price": 13.9,
            "equity": 10000,
            "quantity": 10,
            "max_position_pct": 0.3,
        },
    )

    assert report["final_action"] == "buy"
    assert [item["agent"] for item in report["agents"]] == [
        "technical_analyst",
        "sentiment_analyst",
        "risk_manager",
        "bull_researcher",
        "bear_researcher",
        "trader",
        "portfolio_manager",
    ]


def test_committee_vetoes_oversized_position():
    committee = TradingCommittee()

    report = committee.deliberate(
        _df([10, 10.2, 10.4, 10.8, 11.2, 11.8, 12.3, 12.9, 13.4, 13.9]),
        {
            "ppo_action": 0,
            "rag_score": 7.0,
            "rag_confidence": 0.8,
            "min_rag_confidence": 0.2,
            "current_price": 13.9,
            "equity": 1000,
            "quantity": 100,
            "max_position_pct": 0.3,
        },
    )

    assert report["final_action"] == "hold"
    assert next(item for item in report["agents"] if item["agent"] == "risk_manager")["signal"] == "veto"
