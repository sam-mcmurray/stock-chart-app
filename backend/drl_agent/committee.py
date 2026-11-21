from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd


ACTION_BUY = 0
ACTION_SELL = 1
ACTION_HOLD = 2
ACTION_NAMES = {
    ACTION_BUY: "buy",
    ACTION_SELL: "sell",
    ACTION_HOLD: "hold",
}


@dataclass(frozen=True)
class AgentOpinion:
    agent: str
    signal: str
    score: float
    confidence: float
    rationale: str


def _bounded(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return float(max(low, min(high, value)))


def _safe_last(series: pd.Series, default: float = 0.0) -> float:
    try:
        value = float(series.dropna().iloc[-1])
        if np.isfinite(value):
            return value
    except Exception:
        pass
    return default


def _safe_std(series: pd.Series, default: float = 0.0) -> float:
    try:
        values = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)
        if values.size:
            value = float(np.std(values, ddof=1 if values.size > 1 else 0))
            if np.isfinite(value):
                return value
    except Exception:
        pass
    return default


def _action_from_score(score: float, confidence: float) -> int:
    if confidence < 0.15:
        return ACTION_HOLD
    if score > 0.2:
        return ACTION_BUY
    if score < -0.2:
        return ACTION_SELL
    return ACTION_HOLD


class TechnicalAnalyst:
    name = "technical_analyst"

    def evaluate(self, df: pd.DataFrame, context: dict[str, Any]) -> AgentOpinion:
        close = df["Close"].astype(float)
        if len(close) < 6:
            return AgentOpinion(self.name, "hold", 0.0, 0.1, "insufficient price history")

        short_ma = _safe_last(close.rolling(5).mean(), _safe_last(close))
        long_window = min(20, len(close))
        long_ma = _safe_last(close.rolling(long_window).mean(), short_ma)
        momentum = (close.iloc[-1] - close.iloc[-min(6, len(close))]) / max(abs(close.iloc[-min(6, len(close))]), 1e-9)
        trend = (short_ma - long_ma) / max(abs(long_ma), 1e-9)
        score = _bounded((trend * 6.0) + (momentum * 3.0))
        signal = ACTION_NAMES[_action_from_score(score, 0.75)]
        return AgentOpinion(self.name, signal, score, 0.75, "moving-average trend and recent momentum")


class SentimentAnalyst:
    name = "sentiment_analyst"

    def evaluate(self, df: pd.DataFrame, context: dict[str, Any]) -> AgentOpinion:
        rag_score = float(context.get("rag_score") or 0.0)
        rag_confidence = float(context.get("rag_confidence") or 0.0)
        score = _bounded((rag_score - 2.0) / 8.0)
        signal = ACTION_NAMES[_action_from_score(score, rag_confidence)]
        return AgentOpinion(self.name, signal, score, rag_confidence, "RAG/news confidence signal")


class RiskManager:
    name = "risk_manager"

    def evaluate(self, df: pd.DataFrame, context: dict[str, Any]) -> AgentOpinion:
        close = df["Close"].astype(float)
        returns = close.pct_change().dropna()
        volatility = _safe_std(returns.tail(20))
        current_price = float(context.get("current_price") or _safe_last(close))
        equity = float(context.get("equity") or 0.0)
        quantity = int(context.get("quantity") or 0)
        notional = current_price * max(quantity, 0)
        max_position_pct = float(context.get("max_position_pct") or 0.3)
        position_pct = notional / equity if equity > 0 else 0.0

        score = _bounded(0.35 - (volatility * 8.0) - max(0.0, position_pct - max_position_pct) * 3.0)
        signal = "approve" if score >= 0 else "veto"
        confidence = _bounded(abs(score), 0.2, 1.0)
        return AgentOpinion(self.name, signal, score, confidence, "volatility and position-limit check")


class BullResearcher:
    name = "bull_researcher"

    def evaluate(self, df: pd.DataFrame, context: dict[str, Any]) -> AgentOpinion:
        technical = float(context.get("technical_score") or 0.0)
        sentiment = float(context.get("sentiment_score") or 0.0)
        score = _bounded((technical * 0.65) + (sentiment * 0.35))
        return AgentOpinion(self.name, "buy" if score > 0 else "hold", max(score, 0.0), 0.65, "constructive read from analyst evidence")


class BearResearcher:
    name = "bear_researcher"

    def evaluate(self, df: pd.DataFrame, context: dict[str, Any]) -> AgentOpinion:
        technical = float(context.get("technical_score") or 0.0)
        sentiment = float(context.get("sentiment_score") or 0.0)
        risk = float(context.get("risk_score") or 0.0)
        score = _bounded((-technical * 0.55) + (-sentiment * 0.25) + (min(risk, 0.0) * -0.45))
        return AgentOpinion(self.name, "sell" if score > 0 else "hold", score, 0.65, "downside case from weak trend, sentiment, or risk")


class TraderAgent:
    name = "trader"

    def evaluate(self, df: pd.DataFrame, context: dict[str, Any]) -> AgentOpinion:
        ppo_action = int(context.get("ppo_action", ACTION_HOLD))
        bull_score = float(context.get("bull_score") or 0.0)
        bear_score = float(context.get("bear_score") or 0.0)
        net_score = _bounded(bull_score - bear_score)
        committee_action = _action_from_score(net_score, 0.7)
        if committee_action == ACTION_HOLD:
            signal = ACTION_NAMES[ppo_action] if abs(net_score) > 0.1 else "hold"
        else:
            signal = ACTION_NAMES[committee_action]
        return AgentOpinion(self.name, signal, net_score, 0.7, "combines PPO proposal with researcher debate")


class PortfolioManager:
    name = "portfolio_manager"

    def evaluate(self, df: pd.DataFrame, context: dict[str, Any]) -> AgentOpinion:
        trader_signal = str(context.get("trader_signal") or "hold")
        risk_signal = str(context.get("risk_signal") or "veto")
        min_rag_confidence = float(context.get("min_rag_confidence") or 0.0)
        rag_confidence = float(context.get("rag_confidence") or 0.0)
        if trader_signal == "hold":
            return AgentOpinion(self.name, "hold", 0.0, 0.9, "trader did not find a decisive setup")
        if risk_signal == "veto":
            return AgentOpinion(self.name, "hold", -0.5, 0.9, "risk manager vetoed the trade")
        if rag_confidence < min_rag_confidence:
            return AgentOpinion(self.name, "hold", -0.25, 0.8, "RAG confidence below execution threshold")
        return AgentOpinion(self.name, trader_signal, 0.5, 0.8, "approved by trader and risk checks")


class TradingCommittee:
    """TradingAgents-inspired deterministic committee for DRL action gating."""

    def __init__(self) -> None:
        self.technical = TechnicalAnalyst()
        self.sentiment = SentimentAnalyst()
        self.risk = RiskManager()
        self.bull = BullResearcher()
        self.bear = BearResearcher()
        self.trader = TraderAgent()
        self.portfolio = PortfolioManager()

    def deliberate(self, df: pd.DataFrame, context: dict[str, Any]) -> dict[str, Any]:
        opinions: list[AgentOpinion] = []

        technical = self.technical.evaluate(df, context)
        sentiment = self.sentiment.evaluate(df, context)
        opinions.extend([technical, sentiment])

        enriched = {
            **context,
            "technical_score": technical.score,
            "sentiment_score": sentiment.score,
        }
        risk = self.risk.evaluate(df, enriched)
        enriched["risk_score"] = risk.score
        opinions.append(risk)

        bull = self.bull.evaluate(df, enriched)
        bear = self.bear.evaluate(df, enriched)
        enriched.update({"bull_score": bull.score, "bear_score": bear.score})
        opinions.extend([bull, bear])

        trader = self.trader.evaluate(df, enriched)
        enriched.update({"trader_signal": trader.signal, "risk_signal": risk.signal})
        portfolio = self.portfolio.evaluate(df, enriched)
        opinions.extend([trader, portfolio])

        final_action = portfolio.signal if portfolio.signal in {"buy", "sell"} else "hold"
        return {
            "final_action": final_action,
            "agents": [asdict(opinion) for opinion in opinions],
            "model": "deterministic_trading_committee",
        }
