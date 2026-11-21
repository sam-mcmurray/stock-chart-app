from __future__ import annotations

import os
from typing import Any

import pandas as pd

from services.decision_engine import intraday_snapshot, normalize_ohlcv, technical_snapshot


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    value = 100 - (100 / (1 + rs))
    try:
        return round(float(value.dropna().iloc[-1]), 3)
    except Exception:
        return 50.0


def _momentum_pct(close: pd.Series, lookback: int) -> float:
    if len(close) <= lookback:
        return 0.0
    prev = float(close.iloc[-lookback - 1])
    latest = float(close.iloc[-1])
    return round(((latest - prev) / prev * 100), 3) if prev else 0.0


def market_signal(
    ticker: str,
    df: pd.DataFrame,
    *,
    price: float | None = None,
    rvol: float = 0.0,
    macd_open: bool = False,
    intraday_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    frame = normalize_ohlcv(df)
    close = frame["close"]
    latest = float(price if price is not None else close.iloc[-1])
    technicals = technical_snapshot(frame, rvol, macd_open, price=latest)
    intraday = intraday_snapshot(frame, latest, intraday_df=intraday_df)
    returns = close.pct_change().dropna()
    volatility_20 = float(returns.tail(20).std() or 0.0)
    volume = frame["volume"]
    avg_volume_20 = float(volume.tail(20).mean() or 0.0)
    liquidity_score = min(1.0, avg_volume_20 / 1_000_000) if avg_volume_20 else 0.0

    trend_score = 0.0
    if technicals["above_sma5"]:
        trend_score += 0.25
    if technicals["above_sma20"]:
        trend_score += 0.25
    if macd_open:
        trend_score += 0.25
    trend_score += max(-0.25, min(0.25, _momentum_pct(close, 5) / 20))

    risk_flags: list[str] = []
    rvol_threshold = float(os.getenv("RVOL_THRESHOLD", "3.0"))
    large_rvol = float(rvol or 0.0) >= rvol_threshold
    if not large_rvol:
        risk_flags.append("rvol_below_3x")
    if volatility_20 > 0.08:
        risk_flags.append("high_volatility")
    if liquidity_score < 0.2:
        risk_flags.append("low_liquidity")
    if intraday["halt_risk"]:
        risk_flags.append("halt_risk")
    if intraday["near_low"]:
        risk_flags.append("near_day_low")

    return {
        "ticker": ticker.upper(),
        "price": round(latest, 4),
        "rvol": round(float(rvol or 0.0), 3),
        "rvol_threshold": round(rvol_threshold, 3),
        "large_rvol": large_rvol,
        "trend_score": round(max(-1.0, min(1.0, trend_score)), 3),
        "momentum_5d_pct": _momentum_pct(close, 5),
        "momentum_20d_pct": _momentum_pct(close, 20),
        "rsi_14": _rsi(close),
        "volatility_20": round(volatility_20, 5),
        "liquidity_score": round(liquidity_score, 3),
        "risk_flags": risk_flags,
        "technicals": technicals,
        "intraday": intraday,
    }
