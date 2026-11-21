from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np


POSITIVE_TERMS = {
    "approval", "approved", "beat", "breakthrough", "contract", "deal",
    "earnings beat", "fda", "guidance raised", "merger", "partnership",
    "positive", "raises", "record", "upgrade", "wins"
}

CATALYST_RULES = {
    "fda": {
        "weight": 0.24,
        "direction": "positive",
        "terms": ("fda", "phase 1", "phase 2", "phase 3", "approval", "approved", "clinical trial", "pdufa"),
    },
    "earnings": {
        "weight": 0.14,
        "direction": "mixed",
        "terms": ("earnings", "revenue", "eps", "guidance", "quarterly results", "fiscal"),
    },
    "contract": {
        "weight": 0.18,
        "direction": "positive",
        "terms": ("contract", "purchase order", "agreement", "selected by", "award", "awarded"),
    },
    "partnership": {
        "weight": 0.16,
        "direction": "positive",
        "terms": ("partnership", "collaboration", "strategic alliance", "joint venture"),
    },
    "merger_acquisition": {
        "weight": 0.20,
        "direction": "positive",
        "terms": ("merger", "acquisition", "acquire", "buyout", "takeover", "definitive agreement"),
    },
    "analyst": {
        "weight": 0.08,
        "direction": "mixed",
        "terms": ("upgrade", "downgrade", "price target", "initiated coverage", "analyst"),
    },
    "offering_dilution": {
        "weight": -0.28,
        "direction": "negative",
        "terms": ("offering", "registered direct", "warrants", "atm", "private placement", "dilution"),
    },
    "legal_regulatory": {
        "weight": -0.20,
        "direction": "negative",
        "terms": ("lawsuit", "investigation", "sec", "doj", "probe", "subpoena", "settlement"),
    },
    "distress": {
        "weight": -0.35,
        "direction": "negative",
        "terms": ("bankruptcy", "delisting", "going concern", "default", "restructuring", "reverse split"),
    },
    "insider_institutional": {
        "weight": 0.08,
        "direction": "mixed",
        "terms": ("insider", "institutional", "stake", "13d", "13g", "activist"),
    },
}
NEGATIVE_TERMS = {
    "bankruptcy", "downgrade", "dilution", "investigation", "lawsuit",
    "loss", "miss", "offering", "probe", "recall", "restructuring",
    "sec", "warning"
}


@dataclass
class DecisionConfig:
    min_rvol: float = 3.0
    max_float: int = 20_000_000
    min_risk_reward: float = 1.5
    max_stop_loss_pct: float = 0.12
    max_target_pct: float = 0.25
    min_confidence: float = 0.55


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Return a lowercase OHLCV frame regardless of source column casing."""
    column_map = {
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
        "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
    }
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        first_level = [str(column[0]) for column in out.columns]
        if len(set(first_level)) != len(first_level):
            raise ValueError("multi-symbol OHLCV frames must be reduced to one symbol")
        out.columns = first_level
    out = out.rename(columns={k: v for k, v in column_map.items() if k in out.columns})
    for col in ("open", "high", "low", "close", "volume"):
        if col not in out.columns:
            out[col] = 0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    return out


def _values(series: pd.Series) -> np.ndarray:
    return pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)


def _mean(series: pd.Series, default: float = 0.0) -> float:
    arr = _values(series)
    return float(np.mean(arr)) if arr.size else default


def _min(series: pd.Series, default: float = 0.0) -> float:
    arr = _values(series)
    return float(np.min(arr)) if arr.size else default


def _max(series: pd.Series, default: float = 0.0) -> float:
    arr = _values(series)
    return float(np.max(arr)) if arr.size else default


def _sum(series: pd.Series, default: float = 0.0) -> float:
    arr = _values(series)
    return float(np.sum(arr)) if arr.size else default


def news_sentiment(articles: List[Dict[str, Any]]) -> Dict[str, Any]:
    text = " ".join(
        f"{a.get('title','')} {a.get('summary','')} {a.get('text','')}" for a in articles
    ).lower()
    pos_hits = sorted(term for term in POSITIVE_TERMS if re.search(rf"\b{re.escape(term)}\b", text))
    neg_hits = sorted(term for term in NEGATIVE_TERMS if re.search(rf"\b{re.escape(term)}\b", text))
    score = 0.0 if not pos_hits and not neg_hits else (len(pos_hits) - len(neg_hits)) / max(len(pos_hits) + len(neg_hits), 1)
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return {
        "label": label,
        "score": round(score, 3),
        "assessed": bool(articles),
        "method": "keyword_rules",
        "positive_hits": pos_hits,
        "negative_hits": neg_hits,
        "article_count": len(articles),
    }


def classify_catalysts(articles: List[Dict[str, Any]]) -> Dict[str, Any]:
    text = " ".join(
        f"{a.get('title','')} {a.get('summary','')} {a.get('text','')}" for a in articles
    ).lower()
    matches = []
    for name, rule in CATALYST_RULES.items():
        terms = sorted(term for term in rule["terms"] if re.search(rf"\b{re.escape(term)}\b", text))
        if terms:
            matches.append({
                "type": name,
                "direction": rule["direction"],
                "weight": rule["weight"],
                "terms": terms,
            })

    if not matches:
        return {
            "primary": "unknown",
            "direction": "neutral",
            "score": 0.0,
            "matches": [],
        }

    primary = sorted(matches, key=lambda item: abs(item["weight"]), reverse=True)[0]
    score = sum(item["weight"] for item in matches)
    score = max(-0.5, min(0.5, score))
    direction = "positive" if score > 0.05 else "negative" if score < -0.05 else "neutral"
    return {
        "primary": primary["type"],
        "direction": direction,
        "score": round(score, 3),
        "matches": matches,
    }


def technical_snapshot(
    df: pd.DataFrame,
    rvol: float,
    macd_open: bool,
    price: Optional[float] = None,
) -> Dict[str, Any]:
    frame = normalize_ohlcv(df)
    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"]
    last = float(price) if price is not None else float(close.iloc[-1]) if len(close) else 0.0
    if price is not None and len(close):
        prev = float(close.iloc[-1])
    else:
        prev = float(close.iloc[-2]) if len(close) > 1 else last
    change_pct = ((last - prev) / prev * 100) if prev else 0.0
    sma5 = _mean(close.tail(5)) if len(close) else 0.0
    sma20 = _mean(close.tail(20), sma5) if len(close) else sma5
    avg_range = _mean((high - low).tail(14)) if len(close) else 0.0
    volatility_pct = (avg_range / last) if last else 0.0
    volume_mean_20 = _mean(volume.tail(20))
    volume_trend = _mean(volume.tail(5)) / volume_mean_20 if len(volume) >= 20 and volume_mean_20 else 0.0
    return {
        "last": round(last, 4),
        "change_pct": round(change_pct, 3),
        "sma5": round(sma5, 4),
        "sma20": round(sma20, 4),
        "above_sma5": last > sma5 if sma5 else False,
        "above_sma20": last > sma20 if sma20 else False,
        "rvol": round(float(rvol or 0), 3),
        "volume_trend": round(volume_trend, 3),
        "macd_open": bool(macd_open),
        "volatility_pct": round(volatility_pct, 4),
    }


def market_session(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    weekday = eastern.weekday()
    regular_open = time(9, 30)
    regular_close = time(16, 0)
    premarket_open = time(4, 0)
    postmarket_close = time(20, 0)
    local_time = eastern.time()
    if weekday >= 5:
        state = "closed"
    elif regular_open <= local_time < regular_close:
        state = "open"
    elif premarket_open <= local_time < regular_open:
        state = "premarket"
    elif regular_close <= local_time < postmarket_close:
        state = "afterhours"
    else:
        state = "closed"
    return {
        "state": state,
        "is_regular": state == "open",
        "is_extended": state in ("premarket", "afterhours"),
        "timestamp_utc": now.isoformat(),
        "timestamp_et": eastern.isoformat(),
    }


def intraday_snapshot(
    df: pd.DataFrame,
    price: float,
    *,
    intraday_df: Optional[pd.DataFrame] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    daily = normalize_ohlcv(df)
    session = market_session(now)
    prev_close = float(daily["close"].iloc[-2]) if len(daily) > 1 else float(daily["close"].iloc[-1])
    day_open = float(daily["open"].iloc[-1]) if len(daily) else price
    day_high = float(daily["high"].iloc[-1]) if len(daily) else price
    day_low = float(daily["low"].iloc[-1]) if len(daily) else price
    day_volume = float(daily["volume"].iloc[-1]) if len(daily) else 0.0

    source = "daily"
    vwap = None
    if intraday_df is not None and not intraday_df.empty:
        intra = normalize_ohlcv(intraday_df)
        source = "intraday"
        analysis_intra = intra
        if "t" in intra.columns:
            market_times = pd.to_datetime(intra["t"], utc=True).dt.tz_convert("America/New_York")
            regular = (market_times.dt.time >= time(9, 30)) & (market_times.dt.time < time(16, 0))
            if regular.any():
                analysis_intra = intra[regular]

        day_open = float(analysis_intra["open"].iloc[0])
        day_high = _max(analysis_intra["high"], price)
        day_low = _min(analysis_intra["low"], price)
        day_volume = _sum(analysis_intra["volume"])
        typical = (analysis_intra["high"] + analysis_intra["low"] + analysis_intra["close"]) / 3
        weighted_volume = _sum(typical * analysis_intra["volume"])
        total_volume = _sum(analysis_intra["volume"])
        vwap = weighted_volume / total_volume if total_volume else None

        if "t" in intra.columns and "t" in daily.columns:
            intra_dates = pd.to_datetime(intra["t"], utc=True).dt.tz_convert("America/New_York").dt.date
            daily_dates = pd.to_datetime(daily["t"], utc=True).dt.tz_convert("America/New_York").dt.date
            session_date = intra_dates.max()
            prior_closes = daily.loc[daily_dates < session_date, "close"]
            if not prior_closes.empty:
                prev_close = float(prior_closes.iloc[-1])

    avg_daily_volume = _mean(daily["volume"].tail(20)) if len(daily) >= 20 else _mean(daily["volume"])
    gap_pct = ((day_open - prev_close) / prev_close * 100) if prev_close else 0.0
    open_change_pct = ((price - day_open) / day_open * 100) if day_open else 0.0
    day_range_pct = ((day_high - day_low) / max(day_low, 0.01) * 100) if day_low else 0.0
    volume_pace = day_volume / avg_daily_volume if avg_daily_volume else 0.0
    above_vwap = None if vwap is None else price >= vwap
    near_high = price >= day_high * 0.97 if day_high else False
    near_low = price <= day_low * 1.03 if day_low else False
    halt_risk = day_range_pct >= 35 or abs(gap_pct) >= 80

    return {
        "source": source,
        "session": session,
        "previous_close": round(prev_close, 4),
        "day_open": round(day_open, 4),
        "day_high": round(day_high, 4),
        "day_low": round(day_low, 4),
        "day_volume": int(day_volume),
        "avg_daily_volume_20": int(avg_daily_volume),
        "gap_pct": round(gap_pct, 3),
        "open_change_pct": round(open_change_pct, 3),
        "day_range_pct": round(day_range_pct, 3),
        "volume_pace": round(volume_pace, 3),
        "vwap": round(vwap, 4) if vwap else None,
        "above_vwap": above_vwap,
        "near_high": near_high,
        "near_low": near_low,
        "halt_risk": halt_risk,
    }


def trade_plan(price: float, df: pd.DataFrame, sentiment: Dict[str, Any], config: DecisionConfig) -> Dict[str, Any]:
    frame = normalize_ohlcv(df)
    lows = frame["low"].tail(10)
    highs = frame["high"].tail(10)
    support = _min(lows, price * (1 - config.max_stop_loss_pct)) if len(lows) else price * (1 - config.max_stop_loss_pct)
    max_stop = price * (1 - config.max_stop_loss_pct)
    stop_loss = max(max_stop, support * 0.98)
    stop_loss = min(stop_loss, price * 0.98)
    risk = max(price - stop_loss, price * 0.02)
    multiplier = 2.2 if sentiment.get("label") == "positive" else 1.6 if sentiment.get("label") == "neutral" else 1.1
    reward_target = price + risk * multiplier
    overhead = [value for value in _values(highs) if price < value <= price * (1 + config.max_target_pct)]
    resistance = min(overhead) if overhead else reward_target
    take_profit = min(max(resistance, reward_target), price * (1 + config.max_target_pct))
    risk_reward = (take_profit - price) / risk if risk > 0 else 0.0
    return {
        "entry_reference": round(price, 4),
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        "risk_per_share": round(risk, 4),
        "risk_reward": round(risk_reward, 2),
        "support": round(support, 4),
        "resistance": round(resistance, 4),
    }


def decide(
    symbol: str,
    df: pd.DataFrame,
    *,
    price: Optional[float] = None,
    rvol: float = 0.0,
    float_shares: Optional[int] = None,
    macd_open: bool = False,
    articles: Optional[List[Dict[str, Any]]] = None,
    intraday_df: Optional[pd.DataFrame] = None,
    config: Optional[DecisionConfig] = None,
) -> Dict[str, Any]:
    config = config or DecisionConfig()
    frame = normalize_ohlcv(df)
    latest = float(price if price is not None else frame["close"].iloc[-1])
    articles = articles or []
    sentiment = news_sentiment(articles)
    catalyst = classify_catalysts(articles)
    tech = technical_snapshot(frame, rvol, macd_open, price=latest)
    intraday = intraday_snapshot(frame, latest, intraday_df=intraday_df)
    plan = trade_plan(latest, frame, sentiment, config)

    reasons: List[str] = []
    score = 0.0
    score += min(float(rvol or 0) / max(config.min_rvol, 0.01), 2.0) * 0.25
    score += 0.18 if macd_open else -0.10
    score += 0.15 if tech["above_sma5"] else -0.05
    score += 0.12 if tech["above_sma20"] else -0.04
    score += max(min(sentiment["score"], 1), -1) * 0.20
    score += catalyst["score"]
    if intraday["session"]["is_regular"]:
        score += 0.05
    if intraday["near_high"]:
        score += 0.08
    if intraday["near_low"]:
        score -= 0.08
    if intraday["above_vwap"] is True:
        score += 0.08
    elif intraday["above_vwap"] is False:
        score -= 0.08
    if intraday["volume_pace"] >= 1.5:
        score += 0.06

    if float_shares:
        float_score = 1 - min(float_shares / max(config.max_float, 1), 2.0) / 2
        score += float_score * 0.10
    else:
        reasons.append("float_unknown")
        score -= 0.04

    if plan["risk_reward"] >= config.min_risk_reward:
        score += 0.10
    else:
        reasons.append("risk_reward_below_min")
        score -= 0.15

    if sentiment["label"] == "negative":
        reasons.append("negative_news_terms")
    if catalyst["direction"] == "negative":
        reasons.append(f"negative_catalyst:{catalyst['primary']}")
    if catalyst["primary"] == "unknown":
        reasons.append("catalyst_unknown")
    if intraday["halt_risk"]:
        reasons.append("halt_risk")
    if intraday["session"]["state"] in ("closed", "premarket", "afterhours"):
        reasons.append(f"market_{intraday['session']['state']}")
    if not macd_open:
        reasons.append("macd_not_open")
    if rvol < config.min_rvol:
        reasons.append("rvol_below_min")
    if float_shares and float_shares > config.max_float:
        reasons.append("float_above_max")

    confidence = max(0.0, min(1.0, 0.5 + score / 2))
    action = "watch"
    blocking_prefixes = ("negative_catalyst:",)
    has_blocking_catalyst = any(reason.startswith(blocking_prefixes) for reason in reasons)
    if confidence >= config.min_confidence and not has_blocking_catalyst and not {"negative_news_terms", "macd_not_open", "rvol_below_min", "float_above_max", "float_unknown"}.intersection(reasons):
        action = "long_candidate"
    elif "negative_news_terms" in reasons or has_blocking_catalyst or plan["risk_reward"] < 1:
        action = "avoid"

    return {
        "symbol": symbol.upper(),
        "action": action,
        "confidence": round(confidence, 3),
        "score": round(score, 3),
        "reasons": reasons,
        "sentiment": sentiment,
        "catalyst": catalyst,
        "technicals": tech,
        "intraday": intraday,
        "trade_plan": plan,
        "guardrails": {
            "min_rvol": config.min_rvol,
            "max_float": config.max_float,
            "min_risk_reward": config.min_risk_reward,
            "max_stop_loss_pct": config.max_stop_loss_pct,
            "max_target_pct": config.max_target_pct,
            "min_confidence": config.min_confidence,
        },
    }
