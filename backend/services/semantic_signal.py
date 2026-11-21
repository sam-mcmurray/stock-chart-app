from __future__ import annotations

from typing import Any

from services.decision_engine import classify_catalysts, news_sentiment
from services.news_hype import build_news_hype_features


def semantic_signal(
    ticker: str,
    *,
    articles: list[dict[str, Any]] | None = None,
    rag_signal: dict[str, Any] | None = None,
    as_of_ts: int | None = None,
    market_observations: list[dict[str, Any]] | None = None,
    lookback_seconds: int = 86_400,
) -> dict[str, Any]:
    articles = articles or []
    rag_signal = rag_signal or {}
    sentiment = news_sentiment(articles)
    catalyst = classify_catalysts(articles)
    confidence = float(rag_signal.get("confidence") or 0.0)
    if articles:
        confidence = max(confidence, min(0.95, 0.35 + 0.08 * len(articles)))
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    risk_flags: list[str] = []
    if sentiment["label"] == "negative":
        risk_flags.append("negative_news")
    if catalyst["direction"] == "negative":
        risk_flags.append(f"negative_catalyst:{catalyst['primary']}")
    if catalyst["primary"] == "unknown":
        risk_flags.append("unknown_catalyst")

    event_type = catalyst["primary"]
    direction = catalyst["direction"]
    if sentiment["label"] != "neutral" and direction == "neutral":
        direction = sentiment["label"]

    return {
        "ticker": ticker.upper(),
        "event_type": event_type,
        "sentiment": sentiment["label"],
        "sentiment_score": sentiment["score"],
        "direction": direction,
        "confidence": confidence,
        "time_horizon": "1d-1w",
        "risk_flags": risk_flags,
        "catalyst": catalyst,
        "article_count": len(articles),
        "hype": build_news_hype_features(
            ticker,
            articles=articles,
            as_of_ts=as_of_ts,
            market_observations=market_observations,
            lookback_seconds=lookback_seconds,
        ),
        "rag": {
            "hit_count": int(rag_signal.get("hit_count") or 0),
            "graph_count": int(rag_signal.get("graph_count") or 0),
            "confidence": float(rag_signal.get("confidence") or 0.0),
        },
    }
