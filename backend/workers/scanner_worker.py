import hashlib
import math
from services.scanner_float import resolve_float, share_count
from services.scanner_company_cache import CompanyCache
import logging
import os, time, json, re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import redis
import finnhub
import yfinance as yf
from routers.auth import get_market_data_access_token
from schwab.market_data import SchwabMarketDataClient
from schwab.user_preferences import UserPreferences
from services.decision_engine import DecisionConfig, decide
from services.financial_article_analysis import analyze_candidate_articles, apply_analysis_guardrail, should_analyze_candidate
from services.decision_history import record_decision
from services.feature_store import record_feature_snapshot
from services.live_research_experiment import record_scanner_candidate
from services.shadow_trade_store import upsert_shadow_opportunity
from services.market_signal import market_signal
from services.semantic_signal import semantic_signal
from services import persistent_store
from services.ticker_utils import is_plausible_ticker
from services.calendar_relevance import is_automatically_accepted
from services.calendar_relevance_queue import ACCEPTED_TICKERS_KEY, ENRICHMENTS_KEY
from services.security_master import security_company_identity
from services.company_relationship_graph import related_security_symbols

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)
logger = logging.getLogger(__name__)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

Z_TICKERS = ACCEPTED_TICKERS_KEY
H_SCAN_RESULTS = "scanner:focus"          # hash key -> {json: result}
H_SCAN_PREFIX = "scanner:watchlist:"      # hash key prefix -> bucket -> symbol json
H_SCAN_SETTINGS = "scanner:settings"
H_SCAN_HEARTBEAT = "scanner:heartbeat"
Z_SCAN_LAST_SCANNED = "scanner:last_scanned"
H_SCAN_UNIVERSE_QUOTES = "scanner:universe_quotes"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
SCANNER_MAX_TICKERS_PER_CYCLE = int(os.getenv("SCANNER_MAX_TICKERS_PER_CYCLE", "40"))
SCANNER_NEWS_LOOKBACK_DAYS = max(1, min(30, int(os.getenv("SCANNER_NEWS_LOOKBACK_DAYS", "7"))))
SCANNER_HOT_SYMBOLS_PER_CYCLE = max(0, int(os.getenv("SCANNER_HOT_SYMBOLS_PER_CYCLE", "10")))
SCANNER_MOVER_SYMBOLS_PER_CYCLE = max(0, int(os.getenv("SCANNER_MOVER_SYMBOLS_PER_CYCLE", "10")))
SCANNER_QUOTE_BATCH_SIZE = max(1, min(500, int(os.getenv("SCANNER_QUOTE_BATCH_SIZE", "500"))))
SCANNER_ARTICLE_INDEX_TTL_SECONDS = max(30, int(os.getenv("SCANNER_ARTICLE_INDEX_TTL_SECONDS", "120")))
SCANNER_CALENDAR_LOOKAHEAD_DAYS = max(1, int(os.getenv("SCANNER_CALENDAR_LOOKAHEAD_DAYS", "7")))
SCANNER_GRAPH_RELATED_LIMIT = max(0, int(os.getenv("SCANNER_GRAPH_RELATED_LIMIT", "250")))
SCANNER_GRAPH_RELATED_PER_SYMBOL = max(0, int(os.getenv("SCANNER_GRAPH_RELATED_PER_SYMBOL", "5")))
INTRADAY_RESOLUTION = os.getenv("INTRADAY_RESOLUTION", "5")
INTRADAY_LOOKBACK_DAYS = int(os.getenv("INTRADAY_LOOKBACK_DAYS", "2"))
PRICE_MIN = float(os.getenv("PRICE_MIN", "1.50"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "10.00"))
RVOL_THRESHOLD = float(os.getenv("RVOL_THRESHOLD", os.getenv("AVR_THRESHOLD", "3.0")))
RVOL_WINDOW_DAYS = int(os.getenv("RVOL_WINDOW_DAYS", "90"))
FLOAT_MAX = int(os.getenv("FLOAT_MAX", "20000000"))
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
LOOKBACK_DAYS = max(RVOL_WINDOW_DAYS + 10, 120)
WATCHLISTS = ("too_expensive", "lower_volume", "higher_float", "priority", "actionable")
COMPANY_INFO_CACHE_SECONDS = int(os.getenv("COMPANY_INFO_CACHE_SECONDS", "21600"))
_RVOL_BASELINE_CACHE: dict[str, dict[str, Any]] = {}
_RECENT_ARTICLE_INDEX_CACHE: dict[str, Any] = {
    "loaded_at": 0.0,
    "redis_id": None,
    "by_ticker": {},
}
_CALENDAR_CANDIDATE_CACHE: dict[str, Any] = {"loaded_at": 0.0, "symbols": []}
_GRAPH_CANDIDATE_CACHE: dict[str, Any] = {
    "loaded_at": 0.0, "seed_key": (), "relationships": {},
}
WATCH_SOURCE_LABELS = {
    "recent_news": "Verified recent company news",
    "calendar_catalyst": "Calendar or earnings catalyst",
    "schwab_mover": "Schwab market movers",
    "direct_lookup": "Direct scanner lookup",
}

def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())
def _from_ts(days: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

def _get_recent_tickers() -> List[str]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=SCANNER_NEWS_LOOKBACK_DAYS)).timestamp())
    return [
        symbol
        for symbol in r.zrangebyscore(Z_TICKERS, cutoff, _now_ts())
        if is_plausible_ticker(symbol)
    ]


def _calendar_catalyst_tickers() -> list[str]:
    now = time.time()
    if now - float(_CALENDAR_CANDIDATE_CACHE["loaded_at"] or 0) < 300:
        return list(_CALENDAR_CANDIDATE_CACHE["symbols"])
    if not persistent_store.enabled():
        return []
    today = datetime.now(ZoneInfo("America/New_York")).date()
    start_date = (today - timedelta(days=1)).isoformat()
    end_date = (today + timedelta(days=SCANNER_CALENDAR_LOOKAHEAD_DAYS)).isoformat()
    symbols = set()
    try:
        for event in persistent_store.list_calendar_events(
            limit=10_000,
            start_date=start_date,
            end_date=end_date,
        ):
            if (event.get("details") or {}).get("calendar_kind") == "market_news":
                continue
            symbols.update(event.get("symbols") or [])
        for event in persistent_store.list_earnings_events(
            start_date=start_date,
            end_date=end_date,
            limit=5_000,
        ):
            symbols.add(event.get("symbol"))
    except Exception:
        logger.exception("Failed to load scanner calendar catalysts")
        return list(_CALENDAR_CANDIDATE_CACHE["symbols"])
    normalized = sorted({
        str(symbol or "").strip().upper()
        for symbol in symbols
        if is_plausible_ticker(symbol)
    })
    _CALENDAR_CANDIDATE_CACHE.update({"loaded_at": now, "symbols": normalized})
    return normalized


def _schwab_market_mover_tickers(
    market_client: SchwabMarketDataClient,
) -> tuple[list[str], list[str]]:
    symbols = []
    errors = []
    for market in ("NASDAQ", "NYSE"):
        try:
            rows = market_client.get_market_movers(
                market,
                sort="PERCENT_CHANGE_UP",
                frequency=5,
            )
            symbols.extend(
                str(row.get("symbol") or "").strip().upper()
                for row in rows or []
                if isinstance(row, dict) and is_plausible_ticker(row.get("symbol"))
            )
        except Exception as exc:
            errors.append(f"{market} movers: {exc}")
    return list(dict.fromkeys(symbols)), errors


def _graph_related_candidates(seed_symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    seed_key = tuple(sorted(set(seed_symbols)))
    now = time.time()
    if (
        seed_key == _GRAPH_CANDIDATE_CACHE["seed_key"]
        and now - float(_GRAPH_CANDIDATE_CACHE["loaded_at"] or 0) < 300
    ):
        return dict(_GRAPH_CANDIDATE_CACHE["relationships"])
    relationships = related_security_symbols(
        list(seed_key),
        max_per_seed=SCANNER_GRAPH_RELATED_PER_SYMBOL,
        limit=SCANNER_GRAPH_RELATED_LIMIT,
    )
    _GRAPH_CANDIDATE_CACHE.update({
        "loaded_at": now,
        "seed_key": seed_key,
        "relationships": relationships,
    })
    return relationships


def _automatic_candidate_universe(
    market_client: SchwabMarketDataClient,
) -> tuple[list[str], dict[str, list[str]], list[str], list[str]]:
    news = list(dict.fromkeys(reversed(_get_recent_tickers())))
    calendar = _calendar_catalyst_tickers()
    schwab_movers, errors = _schwab_market_mover_tickers(market_client)
    sources: dict[str, set[str]] = {}

    def add(symbols, source):
        for symbol in symbols:
            normalized = str(symbol or "").strip().upper()
            if is_plausible_ticker(normalized):
                sources.setdefault(normalized, set()).add(source)

    add(news, "recent_news")
    add(calendar, "calendar_catalyst")
    add(schwab_movers, "schwab_mover")
    relationships = _graph_related_candidates(list(sources))
    for rows in relationships.values():
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            kind = str(row.get("kind") or "related").replace(" ", "_")
            add([symbol], f"graph_{kind}")
    ordered = list(sources)
    return ordered, {key: sorted(value) for key, value in sources.items()}, schwab_movers, errors


def _price_eligible_symbols(
    symbols: list[str],
    quotes: dict[str, dict[str, Any]],
    settings: dict[str, Any],
) -> list[str]:
    return [
        symbol for symbol in symbols
        if symbol in quotes
        and settings["price_min"] <= float(quotes[symbol].get("price") or 0) <= settings["price_max"]
    ]


def _watch_source_details(sources: list[str] | None) -> list[dict[str, str]]:
    details = []
    for source in dict.fromkeys(sources or []):
        if source.startswith("graph_"):
            relationship = source.removeprefix("graph_").replace("_", " ")
            details.append({
                "type": source,
                "category": "company_relation",
                "label": f"Company relationship: {relationship}",
            })
        else:
            details.append({
                "type": source,
                "category": "discovery",
                "label": WATCH_SOURCE_LABELS.get(source, source.replace("_", " ")),
            })
    return details


def _select_tickers(
    all_tickers: list[str],
    limit: int,
    *,
    hot_symbols: list[str] | None = None,
    mover_symbols: list[str] | None = None,
    trigger_symbols: list[str] | None = None,
) -> list[str]:
    """Keep active signals hot while fairly rotating through the news universe."""
    normalized = list(dict.fromkeys(
        str(symbol).strip().upper() for symbol in all_tickers if is_plausible_ticker(symbol)
    ))
    if not normalized:
        return []
    capacity = max(1, int(limit))
    active = set(normalized)
    # Reserve at least half the budget for least-recently-scanned names when
    # oversubscribed; repeated hot/mover names must not starve quiet candidates.
    priority_budget = capacity // 2 if len(normalized) > capacity else capacity
    selected = list(dict.fromkeys(
        str(symbol).strip().upper() for symbol in (trigger_symbols or [])
        if str(symbol).strip().upper() in active
    ))[:min(priority_budget, SCANNER_MOVER_SYMBOLS_PER_CYCLE)]
    hot_limit = min(max(0, priority_budget - len(selected)), SCANNER_HOT_SYMBOLS_PER_CYCLE)
    hot = list(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in (hot_symbols or [])
        if str(symbol).strip().upper() in active
        and str(symbol).strip().upper() not in selected
    ))[:hot_limit]
    selected.extend(hot)
    mover_limit = min(max(0, priority_budget - len(selected)), SCANNER_MOVER_SYMBOLS_PER_CYCLE)
    movers = list(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in (mover_symbols or [])
        if str(symbol).strip().upper() in active
        and str(symbol).strip().upper() not in selected
    ))[:mover_limit]
    selected.extend(movers)

    try:
        last_scanned = r.zmscore(Z_SCAN_LAST_SCANNED, normalized)
    except Exception:
        last_scanned = [r.zscore(Z_SCAN_LAST_SCANNED, symbol) for symbol in normalized]
    if not isinstance(last_scanned, (list, tuple)) or len(last_scanned) != len(normalized):
        last_scanned = [None] * len(normalized)
    order = {symbol: index for index, symbol in enumerate(normalized)}
    rotation = sorted(
        zip(normalized, last_scanned),
        key=lambda item: (
            float(item[1]) if item[1] is not None else 0.0,
            order[item[0]],
        ),
    )
    for symbol, _last_scan in rotation:
        if symbol not in selected:
            selected.append(symbol)
        if len(selected) >= capacity:
            break
    return selected


def _current_hot_symbols() -> list[str]:
    try:
        if persistent_store.enabled():
            rows = persistent_store.list_scanner_results(focus_only=True)
        else:
            rows = [json.loads(raw) for raw in (r.hvals(H_SCAN_RESULTS) or [])]
    except Exception:
        return []
    rows.sort(
        key=lambda row: float(row.get("score") or row.get("rvol") or row.get("avr") or 0),
        reverse=True,
    )
    return [str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")]


def _quote_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        try:
            value = float(payload.get(key))
            if math.isfinite(value):
                return value
        except (TypeError, ValueError):
            continue
    return None


def _monitor_news_universe_quotes(
    market_client: SchwabMarketDataClient,
    symbols: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Poll the full news universe cheaply so new movers enter the deep scan quickly."""
    previous_raw = r.hmget(H_SCAN_UNIVERSE_QUOTES, symbols) if symbols else []
    previous: dict[str, dict[str, Any]] = {}
    for symbol, raw in zip(symbols, previous_raw):
        try:
            previous[symbol] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            previous[symbol] = {}

    now = _now_ts()
    quotes: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for start in range(0, len(symbols), SCANNER_QUOTE_BATCH_SIZE):
        batch = symbols[start:start + SCANNER_QUOTE_BATCH_SIZE]
        try:
            response = market_client.get_quotes(batch, fields="quote")
        except Exception as exc:
            errors.append(f"quote batch {start // SCANNER_QUOTE_BATCH_SIZE + 1}: {exc}")
            continue
        for symbol in batch:
            instrument = response.get(symbol) if isinstance(response, dict) else None
            if not isinstance(instrument, dict):
                continue
            quote = instrument.get("quote") if isinstance(instrument.get("quote"), dict) else {}
            price = _quote_number(
                quote, "lastPrice", "mark", "regularMarketLastPrice", "closePrice"
            )
            if price is None or price <= 0:
                continue
            prior_price = _quote_number(previous.get(symbol, {}), "price")
            interval_change = (
                (price / prior_price - 1) * 100
                if prior_price is not None and prior_price > 0
                else None
            )
            day_change = _quote_number(
                quote, "netPercentChange", "regularMarketPercentChange"
            )
            quotes[symbol] = {
                "price": price,
                "ts": now,
                "market_ts": int((_quote_number(quote, "quoteTime", "tradeTime") or 0) / 1000) or None,
                "day_change_pct": day_change,
                "interval_change_pct": interval_change,
                "total_volume": _quote_number(quote, "totalVolume"),
                "previous_total_volume": _quote_number(previous.get(symbol, {}), "total_volume"),
                "previous_market_ts": previous.get(symbol, {}).get("market_ts"),
            }
    if quotes:
        r.hset(H_SCAN_UNIVERSE_QUOTES, mapping={
            symbol: json.dumps(payload) for symbol, payload in quotes.items()
        })
    return quotes, errors


def _cache_rvol_baseline(symbol, df, settings):
    """Cache only today's denominator, from the already-fetched daily frame."""
    _RVOL_BASELINE_CACHE.pop(symbol, None)
    window = settings["rvol_window_days"]
    if df is None or len(df) < 20 or "t" not in df:
        return
    stamps = pd.to_datetime(df["t"], utc=True, errors="coerce")
    today = datetime.fromtimestamp(_now_ts(), ZoneInfo("America/New_York")).date()
    if stamps.isna().any() or not stamps.is_monotonic_increasing or stamps.duplicated().any():
        return
    days = stamps.dt.tz_convert("America/New_York").dt.date
    if days.duplicated().any() or (days > today).any():
        return
    volume = _ohlcv_series(df, "v", "Volume")
    if volume is None:
        return
    # Schwab may omit today's daily candle; never include it in the denominator.
    prior_days = days[days < today]
    if prior_days.empty or (today - prior_days.iloc[-1]).days > 4:
        return
    prior = volume[days < today].iloc[-window:]
    if len(prior) < 20 or not all(math.isfinite(v) and v >= 0 for v in prior):
        return
    average = float(prior.mean())
    if math.isfinite(average) and average > 0:
        _RVOL_BASELINE_CACHE[symbol] = {
            "date": today, "window": window, "average": average,
            "sessions": len(prior), "last_date": prior_days.iloc[-1].isoformat(),
        }


def _current_session_rvol(symbol, df, settings, quote):
    """Use today's cumulative volume, never yesterday's final daily candle."""
    _cache_rvol_baseline(symbol, df, settings)
    base = _RVOL_BASELINE_CACHE.get(symbol)
    now = _now_ts()
    today = datetime.fromtimestamp(now, ZoneInfo("America/New_York")).date()
    quote = quote or {}
    stamp = _quote_number(quote, "market_ts")
    volume = _quote_number(quote, "total_volume")
    details = {
        "valid": False,
        "basis": "current_quote_volume_over_prior_daily_average",
        "market_ts": stamp,
        "quote_age_seconds": now - stamp if stamp is not None else None,
        "baseline_sessions": base.get("sessions") if base else None,
        "baseline_last_date": base.get("last_date") if base else None,
        "baseline_average_volume": base["average"] if base else None,
    }
    if not base:
        details["reason"] = "daily_volume_baseline_unavailable"
    elif stamp is None or not -5 <= now - stamp <= 120:
        details["reason"] = "quote_volume_stale_or_missing"
    elif datetime.fromtimestamp(stamp, ZoneInfo("America/New_York")).date() != today:
        details["reason"] = "quote_volume_wrong_session"
    elif volume is None or volume < 0:
        details["reason"] = "quote_volume_invalid"
    else:
        ratio = volume / base["average"]
        if math.isfinite(ratio):
            details.update(valid=True, reason=None, current_volume=volume)
            return ratio, details
        details["reason"] = "quote_volume_invalid"
    return None, details


def _volume_trigger_symbols(quotes, settings):
    """Quote-volume estimates only schedule full RVOL/technical rechecks."""
    now = _now_ts()
    today = datetime.fromtimestamp(now, ZoneInfo("America/New_York")).date()
    ranked = []
    for symbol, quote in quotes.items():
        base = _RVOL_BASELINE_CACHE.get(symbol)
        if not base or base["date"] != today or base["window"] != settings["rvol_window_days"]:
            continue
        stamp = _quote_number(quote, "market_ts")
        price, volume = _quote_number(quote, "price"), _quote_number(quote, "total_volume")
        if (stamp is None or not -5 <= now - stamp <= 120
                or datetime.fromtimestamp(stamp, ZoneInfo("America/New_York")).date() != today
                or price is None or not settings["price_min"] <= price <= settings["price_max"]
                or volume is None or volume < 0):
            continue
        ratio = volume / base["average"]
        previous, previous_ts = _quote_number(quote, "previous_total_volume"), _quote_number(quote, "previous_market_ts")
        comparable = (previous is not None and previous >= 0 and previous <= volume
                      and previous_ts is not None and previous_ts < stamp
                      and stamp - previous_ts <= 180
                      and datetime.fromtimestamp(previous_ts, ZoneInfo("America/New_York")).date() == today)
        crossing = bool(comparable and previous / base["average"] < settings["rvol_threshold"] <= ratio)
        quote.update(rvol_proxy=ratio, rvol_proxy_basis="quote_total_volume_over_cached_prior_daily_average",
                     rvol_crossed_threshold=crossing, rvol_proxy_entry_permission=False)
        if crossing:
            quote["rvol_crossing_detected_ts"] = now
        approaching = comparable and volume > previous and ratio >= settings["rvol_threshold"] * .8
        if ratio >= settings["rvol_threshold"] or approaching:
            ranked.append((crossing, ratio >= settings["rvol_threshold"], ratio, symbol))
    ranked.sort(key=lambda x: (-int(x[0]), -int(x[1]), -x[2], x[3]))
    return [item[3] for item in ranked]


def _audit_volume_crossings(quotes, selected, buckets):
    """Keep bounded scheduling evidence; these records never authorize orders."""
    rows = {symbol: row for items in buckets.values() for symbol, row in items.items()}
    for symbol, quote in quotes.items():
        if not quote.get("rvol_crossed_threshold"):
            continue
        row = rows.get(symbol)
        detected = quote.get("rvol_crossing_detected_ts")
        completed = row.get("evaluation_completed_ts") if row else None
        event = {
            "symbol": symbol, "detected_ts": detected,
            "quote_ts": quote.get("market_ts"), "recorded_ts": _now_ts(),
            "rvol_proxy": quote.get("rvol_proxy"),
            "status": "evaluated" if row else "no_result" if symbol in selected else "deferred",
            "evaluation_completed_ts": completed,
            "detection_to_evaluation_seconds": completed - detected if completed is not None and detected is not None else None,
            "rvol": row.get("rvol") if row else None,
            "rvol_valid": row.get("rvol_valid") if row else None,
            "reason": row.get("reason") if row else None,
            "action": (row.get("decision") or {}).get("action") if row else None,
            "entry_permission": False,
        }
        try:
            r.xadd("scanner:volume_crossing_audit", {"event": json.dumps(event)},
                   maxlen=1000, approximate=False)
        except Exception:
            logger.exception("Failed to audit scanner volume crossing for %s", symbol)


def _moving_symbols(
    quotes: dict[str, dict[str, Any]], settings: dict[str, Any]
) -> list[str]:
    eligible = [
        (symbol, payload)
        for symbol, payload in quotes.items()
        if settings["price_min"] <= float(payload.get("price") or 0) <= settings["price_max"]
        and max(
            float(payload.get("interval_change_pct") or 0),
            float(payload.get("day_change_pct") or 0),
        ) > 0
    ]
    eligible.sort(
        key=lambda item: (
            max(0.0, float(item[1].get("interval_change_pct") or 0)) * 3
            + max(0.0, float(item[1].get("day_change_pct") or 0)),
            int(item[1].get("total_volume") or 0),
        ),
        reverse=True,
    )
    return [symbol for symbol, _payload in eligible]

def _settings() -> Dict[str, Any]:
    settings = {
        "price_min": PRICE_MIN,
        "price_max": PRICE_MAX,
        "rvol_threshold": RVOL_THRESHOLD,
        "rvol_window_days": RVOL_WINDOW_DAYS,
        "float_max": FLOAT_MAX,
        "scan_interval": SCAN_INTERVAL,
    }
    pg_settings = None
    try:
        pg_settings = persistent_store.load_scanner_settings()
        if pg_settings:
            settings.update(pg_settings)
    except Exception:
        logger.exception("Failed to load scanner settings from PostgreSQL")

    try:
        raw = r.hgetall(H_SCAN_SETTINGS) or {}
        for key in ("price_min", "price_max", "rvol_threshold"):
            if key in raw and not pg_settings:
                settings[key] = float(raw[key])
        for key in ("rvol_window_days", "float_max", "scan_interval"):
            if key in raw and not pg_settings:
                settings[key] = int(float(raw[key]))
    except Exception:
        pass
    settings["rvol_window_days"] = max(20, min(252, settings["rvol_window_days"]))
    settings["price_min"], settings["price_max"] = sorted((settings["price_min"], settings["price_max"]))
    return settings

def _finnhub_client() -> finnhub.Client:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY not set")
    return finnhub.Client(api_key=FINNHUB_API_KEY)

def _schwab_market_client() -> SchwabMarketDataClient:
    access_token = get_market_data_access_token()
    prefs = UserPreferences(access_token=access_token)
    return SchwabMarketDataClient(user_prefs=prefs, access_token=access_token)

def _get_daily(
    symbol: str,
    market_client: Optional[SchwabMarketDataClient] = None,
) -> Optional[pd.DataFrame]:
    client = market_client or _schwab_market_client()
    months = max(3, (LOOKBACK_DAYS // 30) + 2)
    df = client.get_daily_frame(symbol, months=months)
    return None if df is None or df.empty else df

def _get_intraday(
    symbol: str,
    market_client: Optional[SchwabMarketDataClient] = None,
) -> Optional[pd.DataFrame]:
    try:
        client = market_client or _schwab_market_client()
        df = client.get_intraday_frame(
            symbol,
            days=INTRADAY_LOOKBACK_DAYS,
            frequency=int(INTRADAY_RESOLUTION),
        )
        return None if df is None or df.empty else df
    except Exception:
        logger.debug("Schwab intraday data unavailable for %s", symbol, exc_info=True)
    return None

def _latest_quote(
    symbol: str,
    df: Optional[pd.DataFrame] = None,
    market_client: Optional[SchwabMarketDataClient] = None,
) -> float:
    try:
        client = market_client or _schwab_market_client()
        price = client.get_last_price(symbol)
        if price > 0:
            return price
    except Exception:
        pass
    if df is not None and not df.empty:
        close = _ohlcv_series(df, "c", "Close")
        if close is not None and not close.empty:
            return float(close.iloc[-1])
    return 0.0


def _ohlcv_series(
    df: pd.DataFrame, short_name: str, long_name: str
) -> Optional[pd.Series]:
    """Read one OHLCV field from Schwab or single-symbol Yahoo frames."""
    for name in (short_name, long_name):
        try:
            value = df[name]
        except (KeyError, TypeError):
            continue
        if isinstance(value, pd.DataFrame):
            if value.shape[1] != 1:
                continue
            value = value.iloc[:, 0]
        return pd.to_numeric(value, errors="coerce")
    if isinstance(df.columns, pd.MultiIndex):
        matches = [column for column in df.columns if str(column[0]) == long_name]
        if len(matches) == 1:
            return pd.to_numeric(df[matches[0]], errors="coerce")
    return None

def _avg_volume_ratio(df: pd.DataFrame, window_days: Optional[int] = None) -> float:
    if df is None or df.empty: return 0.0
    window = int(window_days or _settings()["rvol_window_days"])
    if len(df) < min(window, 20) + 2: return 0.0
    volume = _ohlcv_series(df, "v", "Volume")
    if volume is None or volume.empty:
        return 0.0
    cur_vol = float(volume.iloc[-1])
    avg = float(volume.iloc[-window-1:-1].mean())
    return (cur_vol / avg) if avg > 0 else 0.0

def _macd_open(df: pd.DataFrame) -> bool:
    """
    Define "MACD open" as: MACD line above signal AND histogram > 0 on the latest bar.
    Adjust as needed.
    """
    if df is None or len(df) < MACD_SLOW + MACD_SIGNAL + 5:  # safety
        return False
    close = _ohlcv_series(df, "c", "Close")
    if close is None or close.empty:
        return False
    try:
        import pandas_ta as ta
    except Exception:
        ta = None
    if ta is not None:
        macd = ta.macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        if macd is None or macd.empty: return False
        macd_line = float(macd.iloc[-1]["MACD_12_26_9"])
        signal    = float(macd.iloc[-1]["MACDs_12_26_9"])
        hist      = float(macd.iloc[-1]["MACDh_12_26_9"])
        return macd_line > signal and hist > 0
    close = close.astype(float)
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist = macd_line - signal_line
    macd_line = float(macd_line.iloc[-1])
    signal = float(signal_line.iloc[-1])
    hist = float(hist.iloc[-1])
    return macd_line > signal and hist > 0

def _price_ok(df: pd.DataFrame, price: Optional[float] = None, settings: Optional[Dict[str, Any]] = None) -> bool:
    if df is None or df.empty: return False
    settings = settings or _settings()
    close = _ohlcv_series(df, "c", "Close")
    if price is None and (close is None or close.empty):
        return False
    last = float(price if price is not None else close.iloc[-1])
    return settings["price_min"] <= last <= settings["price_max"]

def _score(symbol: str, df: pd.DataFrame, avr: float) -> float:
    # Simple ranking: higher AVR first; you can add momentum/volatility/etc.
    return avr

def _load_company_info(symbol: str) -> Dict[str, Any]:
    normalized = str(symbol).upper()
    identity = security_company_identity(normalized)
    info = {
        "name": identity.get("name") or "",
        "exchange": identity.get("exchange") or "",
        "float_shares": None,
        "shares_outstanding": None,
        "float_source": None,
        "market_cap": None,
        "source": identity.get("source"),
    }
    try:
        profile = _finnhub_client().company_profile2(symbol=normalized)
        info["name"] = profile.get("name") or info["name"]
        market_cap_millions = profile.get("marketCapitalization")
        info["market_cap"] = (
            float(market_cap_millions) * 1_000_000 if market_cap_millions else None
        )
        info["source"] = "finnhub"
    except Exception:
        pass
    try:
        yf_info = yf.Ticker(normalized).fast_info
        shares = getattr(yf_info, "shares", None) or yf_info.get("shares")
        if shares:
            info["shares_outstanding"] = int(shares)
            info["source"] = (info["source"] or "yfinance")
    except Exception:
        pass
    try:
        yf_info = yf.Ticker(normalized).info
        info["name"] = info["name"] or yf_info.get("shortName") or yf_info.get("longName") or ""
        shares = share_count(yf_info.get("floatShares"))
        if shares:
            info["float_shares"] = int(shares)
            info["float_source"] = "yfinance.floatShares"
        if yf_info.get("sharesOutstanding"):
            info["shares_outstanding"] = int(yf_info["sharesOutstanding"])
        info["market_cap"] = info["market_cap"] or yf_info.get("marketCap")
        info["source"] = info["source"] or "yfinance"
    except Exception:
        pass
    info.update(resolve_float(normalized, info["float_shares"], info["shares_outstanding"], r))
    return dict(info)


_COMPANY_CACHE = CompanyCache(_load_company_info, COMPANY_INFO_CACHE_SECONDS)


def _company_info(symbol: str) -> Dict[str, Any]:
    info, status = _COMPANY_CACHE.get(str(symbol).upper())
    info = {"name": "", "exchange": "", "market_cap": None,
            "shares_outstanding": None, "source": None, **info}
    max_age = max(0, min(7 * 86400, int(os.getenv("SCANNER_FLOAT_MAX_CACHE_SECONDS", "86400"))))
    try:
        age = time.time() - float(info.get("float_observed_ts") or 0)
        valid_float = share_count(info.get("float_shares")) and 0 <= age <= max_age
    except (ValueError, TypeError, OverflowError):
        valid_float = False
    if not valid_float:
        if info.get("float_shares") and status == "fresh":
            _, status = _COMPANY_CACHE.get(str(symbol).upper(), force_refresh=True)
        info.update(float_shares=None, float_source=None, float_status="unknown",
                    float_observed_ts=None, float_as_of=None, float_basis=None,
                    float_fallback_reason="metadata_pending_or_float_expired")
    elif status != "fresh":
        info["float_status"] = "cached"
    info["metadata_refresh_status"] = status
    return info


def _article_published_ts(*values: Any) -> int:
    for value in values:
        try:
            numeric = int(float(value))
            if numeric > 0:
                return numeric // 1000 if numeric > 10_000_000_000 else numeric
        except (TypeError, ValueError):
            pass
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                return int(parsed.timestamp())
            except ValueError:
                pass
    return 0

def _article_record(aid: str, raw: str, enrichment_raw: str) -> dict[str, Any] | None:
    try:
        item = json.loads(raw)
        enrichment = json.loads(enrichment_raw)
    except (TypeError, ValueError):
        return None
    if (
        not is_automatically_accepted(enrichment)
        or enrichment.get("relevance_label") not in {"finance", "business", "markets"}
    ):
        return None
    tickers = sorted({
        str(ticker).strip().upper()
        for ticker in enrichment.get("related_tickers") or []
        if is_plausible_ticker(str(ticker).strip().upper())
    })
    if not tickers:
        return None
    article_url = (
        enrichment.get("resolved_article_url")
        or item.get("resolved_article_url")
        or item.get("article_url")
        or item.get("url")
        or enrichment.get("supporting_url")
    )
    published_ts = _article_published_ts(
        item.get("published_ts"), item.get("published_at"), enrichment.get("published_ts")
    )
    if published_ts <= 0:
        return None
    return {
        **item,
        "id": aid,
        "title": enrichment.get("display_headline") or item.get("title") or "",
        "summary": enrichment.get("llm_summary") or item.get("summary") or "",
        "article_url": article_url,
        "resolved_article_url": article_url,
        "published_ts": published_ts,
        "tickers": tickers,
        "relevance_label": enrichment.get("relevance_label"),
        "relevance_confidence": enrichment.get("relevance_confidence"),
        "article_verification_status": enrichment.get("article_verification_status"),
        "related_companies": enrichment.get("related_companies") or [],
        "company_relationships": enrichment.get("company_relationships") or [],
        "ticker_source": "accepted_llm_company_validated",
    }


def _recent_article_index() -> dict[str, list[dict[str, Any]]]:
    now = _now_ts()
    cached = _RECENT_ARTICLE_INDEX_CACHE
    if (
        cached["redis_id"] == id(r)
        and time.time() - float(cached["loaded_at"] or 0) < SCANNER_ARTICLE_INDEX_TTL_SECONDS
    ):
        return cached["by_ticker"]

    cutoff = now - SCANNER_NEWS_LOOKBACK_DAYS * 86_400
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    try:
        ids = r.zrevrangebyscore("news:recent:articles", "+inf", cutoff)
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            if len(batch) <= 200:
                raw_items = [r.hget(f"news:article:{aid}", "json") for aid in batch]
            else:
                pipe = r.pipeline(transaction=False)
                for aid in batch:
                    pipe.hget(f"news:article:{aid}", "json")
                raw_items = pipe.execute()
            enrichment_items = r.hmget(ENRICHMENTS_KEY, batch)
            for aid, raw, enrichment_raw in zip(batch, raw_items, enrichment_items):
                if not raw or not enrichment_raw:
                    continue
                article = _article_record(aid, raw, enrichment_raw)
                if not article or article["published_ts"] < cutoff or article["published_ts"] > now:
                    continue
                for ticker in article["tickers"]:
                    by_ticker.setdefault(ticker, []).append(dict(article))

        for articles in by_ticker.values():
            articles.sort(key=lambda article: int(article["published_ts"]), reverse=True)
            earliest_ts = min(int(article["published_ts"]) for article in articles)
            count = len(articles)
            for article in articles:
                article["coverage_first_published_ts"] = earliest_ts
                article["coverage_count"] = count
                article["coverage_sequence"] = (
                    "earliest_in_lookback"
                    if int(article["published_ts"]) == earliest_ts
                    else "follow_up"
                )
    except Exception:
        logger.exception("Failed to build the seven-day verified article index")
        by_ticker = {}
    cached.update({"loaded_at": time.time(), "redis_id": id(r), "by_ticker": by_ticker})
    return by_ticker


def _recent_articles(symbol: str, limit: int = 5) -> List[Dict[str, Any]]:
    articles = _recent_article_index().get(str(symbol).strip().upper(), [])
    bounded_limit = max(1, int(limit))
    selected = list(articles[:bounded_limit])
    if len(articles) > bounded_limit and bounded_limit > 1:
        earliest = articles[-1]
        selected[-1] = earliest
    return [dict(article) for article in selected]


def _news_watch_summary(articles: list[dict[str, Any]], *, now: int | None = None) -> dict[str, Any]:
    if not articles:
        return {
            "state": "none",
            "lookback_days": SCANNER_NEWS_LOOKBACK_DAYS,
            "article_count": 0,
        }
    current = int(now or _now_ts())
    published = sorted(int(article.get("published_ts") or 0) for article in articles if article.get("published_ts"))
    article_count = max(
        len(articles),
        max((int(article.get("coverage_count") or 0) for article in articles), default=0),
    )
    if not published:
        return {
            "state": "time_unknown",
            "lookback_days": SCANNER_NEWS_LOOKBACK_DAYS,
            "article_count": article_count,
        }
    earliest = min(
        int(article.get("coverage_first_published_ts") or published[0])
        for article in articles
    )
    latest = published[-1]
    expires = latest + SCANNER_NEWS_LOOKBACK_DAYS * 86_400
    return {
        "state": "fresh" if current - latest <= 86_400 else "watching",
        "lookback_days": SCANNER_NEWS_LOOKBACK_DAYS,
        "article_count": article_count,
        "earliest_published_ts": earliest,
        "latest_published_ts": latest,
        "latest_age_seconds": max(0, current - latest),
        "watch_expires_ts": expires,
    }


def _attach_gdelt_mentions(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def article_event_ids(article: dict[str, Any]) -> list[Any]:
        values = article.get("event_ids") or []
        if not isinstance(values, list):
            values = [values]
        return values + ([article.get("event_id")] if article.get("event_id") else [])

    event_ids = sorted({
        str(event_id)
        for article in articles
        for event_id in article_event_ids(article)
        if str(event_id).isdigit()
    })
    if not event_ids:
        return articles
    mentions = persistent_store.list_gdelt_mentions_for_events(event_ids)
    by_event: dict[str, list[dict[str, Any]]] = {}
    for mention in mentions:
        by_event.setdefault(str(mention.get("event_id") or ""), []).append(mention)
    return [
        {
            **article,
            "gdelt_mentions": [
                mention
                for event_id in article_event_ids(article)
                for mention in by_event.get(str(event_id), [])
            ],
        }
        for article in articles
    ]


def _record_shadow_candidate(
    symbol: str,
    *,
    ts: int,
    df: pd.DataFrame,
    intraday: pd.DataFrame | None,
    price: float,
    rvol: float,
    macd_open: bool,
    articles: list[dict[str, Any]],
    decision: dict[str, Any],
    float_shares: float | None = None,
    scanner_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hydrated_articles = _attach_gdelt_mentions(articles)
    observations = persistent_store.list_market_observations_as_of(
        symbol,
        as_of_ts=ts,
        start_ts=ts - 86_400,
        observation_type="minute_candle",
        limit=2_000,
    )
    semantic = semantic_signal(
        symbol,
        articles=hydrated_articles,
        as_of_ts=ts,
        market_observations=observations,
        lookback_seconds=SCANNER_NEWS_LOOKBACK_DAYS * 86_400,
    )
    market = market_signal(
        symbol,
        df,
        price=price,
        rvol=rvol,
        macd_open=macd_open,
        intraday_df=intraday,
    )
    evidence_ids = sorted({
        str(value).strip()
        for value in ((semantic.get("hype") or {}).get("evidence_article_ids") or [])
        if str(value).strip()
    })
    opportunity_key = None
    if evidence_ids:
        identity = json.dumps(
            {"symbol": symbol.upper(), "evidence_article_ids": evidence_ids},
            sort_keys=True, separators=(",", ":"),
        )
        opportunity_key = "shadow-opportunity:" + hashlib.sha256(
            identity.encode()
        ).hexdigest()
    payload = {
        "symbol": symbol,
        "ts": ts,
        "source": "scanner_shadow",
        "shadow_candidate": True,
        "shadow_opportunity_key": opportunity_key,
        "ranking_only": True,
        "live_activation_eligible": False,
        "shadow_stage": decision.get("shadow_stage") or "actionable",
        "entry_eligible": False,
        "watch_inputs": {
            "version": "scanner-watch-inputs-v1",
            "observed_at": ts,
            "price": price,
            "rvol": rvol,
            "float_shares": float_shares,
            "scanner_settings": dict(scanner_settings or {}),
        },
        "action": decision.get("action") or "watch",
        "semantic_signal": semantic,
        "market_signal": market,
        "trade_plan": decision.get("trade_plan") or {},
        "decision": decision,
    }
    snapshot = record_feature_snapshot(
        symbol,
        payload,
        source="scanner_shadow",
        ts=ts,
    )
    if os.getenv('LEGACY_RESEARCH_ENABLED', 'true').lower() != 'true':
        return snapshot
    if opportunity_key:
        try:
            snapshot["shadow_opportunity"] = upsert_shadow_opportunity(snapshot)
        except Exception:
            logger.exception("Failed to upsert shadow opportunity for %s", symbol)
    try:
        snapshot["live_research"] = record_scanner_candidate(snapshot)
    except Exception:
        logger.exception("Failed to record live research candidate for %s", symbol)
    return snapshot

def _classify(
    symbol: str,
    df: pd.DataFrame,
    settings: Dict[str, Any],
    market_client: Optional[SchwabMarketDataClient] = None,
    quote_snapshot: dict[str, Any] | None = None,
) -> tuple[str, Dict[str, Any]]:
    price = float((quote_snapshot or {}).get("price") or 0) or _latest_quote(
        symbol, df, market_client=market_client
    )
    current_rvol, rvol_details = _current_session_rvol(symbol, df, settings, quote_snapshot)
    # Existing decision/scoring APIs take a number; unknown data must fail closed.
    rvol = current_rvol if current_rvol is not None else 0.0
    company = _company_info(symbol)
    float_shares = company.get("float_shares")
    macd_open = _macd_open(df)
    articles = _recent_articles(symbol)
    config = DecisionConfig(
        min_rvol=settings["rvol_threshold"],
        max_float=settings["float_max"],
    )
    decision = decide(
        symbol,
        df,
        price=price,
        rvol=rvol,
        float_shares=float_shares,
        macd_open=macd_open,
        articles=articles,
        intraday_df=None,
        config=config,
    )
    decision["rvol_details"] = rvol_details
    if current_rvol is None:
        decision["reasons"].append("rvol_unavailable")
        decision["technicals"]["rvol"] = None
        if decision["action"] == "long_candidate":
            decision["action"] = "watch"
    row_ts = _now_ts()
    row = {
        "symbol": symbol,
        "last": price,
        "ts": row_ts,
        "price_range": [settings["price_min"], settings["price_max"]],
        "rvol": current_rvol,
        "avr": current_rvol,
        "rvol_valid": rvol_details["valid"],
        "rvol_details": rvol_details,
        "company": company,
        "macd_open": macd_open,
        "decision": decision,
        "news_sentiment": decision["sentiment"],
        "trade_plan": decision["trade_plan"],
        "articles": articles,
        "news_watch": _news_watch_summary(articles, now=row_ts),
        "universe_quote": quote_snapshot or {},
        "score": _score(symbol, df, rvol) + max(decision["score"], 0),
    }
    if price <= 0 or not _price_ok(df, price, settings):
        row["reason"] = "price_out_of_range"
        return "too_expensive", row

    if current_rvol is None or rvol < settings["rvol_threshold"]:
        row["reason"] = "rvol_unavailable" if current_rvol is None else "rvol_below_threshold"
        row["rvol_threshold"] = settings["rvol_threshold"]
        if articles and current_rvol is not None:
            try:
                decision["shadow_stage"] = "low_rvol"
                snapshot = _record_shadow_candidate(
                    symbol,
                    ts=row["ts"],
                    df=df,
                    intraday=None,
                    price=price,
                    rvol=rvol,
                    macd_open=macd_open,
                    articles=articles,
                    decision=decision,
                    float_shares=float_shares,
                    scanner_settings=settings,
                )
                row["shadow_feature_id"] = snapshot.get("id")
            except Exception:
                logger.exception("Failed to record low-RVOL shadow candidate for %s", symbol)
        return "lower_volume", row

    if float_shares and float_shares > settings["float_max"]:
        row["reason"] = "float_above_threshold"
        row["float_max"] = settings["float_max"]
        if articles:
            try:
                decision["shadow_stage"] = "high_float"
                snapshot = _record_shadow_candidate(
                    symbol,
                    ts=row["ts"],
                    df=df,
                    intraday=None,
                    price=price,
                    rvol=rvol,
                    macd_open=macd_open,
                    articles=articles,
                    decision=decision,
                    float_shares=float_shares,
                    scanner_settings=settings,
                )
                row["shadow_feature_id"] = snapshot.get("id")
            except Exception:
                logger.exception("Failed to record high-float shadow candidate for %s", symbol)
        return "higher_float", row

    if not macd_open:
        row["reason"] = "macd_not_open"
        if articles:
            try:
                decision["shadow_stage"] = "macd_closed"
                snapshot = _record_shadow_candidate(
                    symbol,
                    ts=row["ts"],
                    df=df,
                    intraday=None,
                    price=price,
                    rvol=rvol,
                    macd_open=False,
                    articles=articles,
                    decision=decision,
                    float_shares=float_shares,
                    scanner_settings=settings,
                )
                row["shadow_feature_id"] = snapshot.get("id")
            except Exception:
                logger.exception("Failed to record MACD shadow candidate for %s", symbol)
        return "priority", row

    intraday = _get_intraday(symbol, market_client=market_client)
    decision = decide(
        symbol,
        df,
        price=price,
        rvol=rvol,
        float_shares=float_shares,
        macd_open=macd_open,
        articles=articles,
        intraday_df=intraday,
        config=config,
    )
    decision["rvol_details"] = rvol_details
    if should_analyze_candidate(decision):
        analysis = analyze_candidate_articles(symbol, articles)
        apply_analysis_guardrail(decision, analysis)
    row.update({
        "reason": "actionable",
        "score": _score(symbol, df, rvol) + max(decision["score"], 0),
        "decision": decision,
        "news_sentiment": decision["sentiment"],
        "trade_plan": decision["trade_plan"],
        "articles": articles,
    })
    decision["shadow_stage"] = "actionable"
    try:
        record_decision(symbol, decision, source="scanner_worker", watchlist="actionable", ts=row["ts"])
    except Exception:
        pass
    try:
        snapshot = _record_shadow_candidate(
            symbol,
            ts=row["ts"],
            df=df,
            intraday=intraday,
            price=price,
            rvol=rvol,
            macd_open=macd_open,
            articles=articles,
            decision=decision,
            float_shares=float_shares,
            scanner_settings=settings,
        )
        row["shadow_feature_id"] = snapshot.get("id")
    except Exception:
        logger.exception("Failed to record shadow feature snapshot for %s", symbol)
    return "actionable", row

class ScannerWorker:
    def __init__(self):
        self._stop = False

    def stop(self): self._stop = True

    def _heartbeat(self, status: str, **extra):
        payload = {"status": status, "ts": _now_ts(), **extra}
        try:
            r.set(H_SCAN_HEARTBEAT, json.dumps(payload))
        except Exception:
            pass

    def run_forever(self):
        while not self._stop:
            cycle_started_monotonic = time.monotonic()
            try:
                started = _now_ts()
                settings = _settings()
                news_candidate_count = len(_get_recent_tickers())
                try:
                    market_client = _schwab_market_client()
                except RuntimeError as exc:
                    if "No authenticated Schwab session" not in str(exc):
                        raise
                    self._heartbeat(
                        "idle",
                        started_ts=started,
                        market_data_source="schwab",
                        reason="schwab_auth_required",
                        candidate_count=news_candidate_count,
                        ticker_count=0,
                        result_count=0,
                        failed_count=0,
                    )
                    logger.info("Scanner idle until a Schwab session is authenticated")
                    continue
                discovery_tickers, watch_sources, schwab_movers, discovery_errors = (
                    _automatic_candidate_universe(market_client)
                )
                universe_quotes, quote_errors = _monitor_news_universe_quotes(
                    market_client, discovery_tickers
                )
                all_tickers = _price_eligible_symbols(
                    discovery_tickers,
                    universe_quotes,
                    settings,
                )
                eligible = set(all_tickers)
                mover_symbols = list(dict.fromkeys(
                    [symbol for symbol in schwab_movers if symbol in eligible]
                    + _moving_symbols(universe_quotes, settings)
                ))
                volume_triggers = _volume_trigger_symbols(universe_quotes, settings)
                tickers = _select_tickers(
                    all_tickers,
                    SCANNER_MAX_TICKERS_PER_CYCLE,
                    hot_symbols=_current_hot_symbols(),
                    mover_symbols=mover_symbols,
                    trigger_symbols=volume_triggers,
                )
                buckets: Dict[str, Dict[str, Any]] = {name: {} for name in WATCHLISTS}
                failed_count = 0
                error_samples = []
                for sym in tickers:
                    try:
                        df = _get_daily(sym, market_client=market_client)
                        if df is None: continue
                        bucket, row = _classify(
                            sym,
                            df,
                            settings,
                            market_client=market_client,
                            quote_snapshot=universe_quotes.get(sym),
                        )
                        row["market_data_source"] = "schwab"
                        row["evaluation_completed_ts"] = _now_ts()
                        row["watch_sources"] = watch_sources.get(sym, [])
                        row["watch_source_details"] = _watch_source_details(row["watch_sources"])
                        if isinstance(row.get("decision", {}).get("catalyst"), dict):
                            row["decision"]["catalyst"]["sources"] = row["watch_source_details"]
                        buckets[bucket][sym] = row
                    except Exception as exc:
                        failed_count += 1
                        if len(error_samples) < 3:
                            error_samples.append(f"{sym}: {exc}")
                        continue

                if tickers:
                    r.zadd(Z_SCAN_LAST_SCANNED, {symbol: _now_ts() for symbol in tickers})
                r.zremrangebyscore(
                    Z_SCAN_LAST_SCANNED,
                    "-inf",
                    _now_ts() - max(30, SCANNER_NEWS_LOOKBACK_DAYS * 3) * 86_400,
                )

                if persistent_store.enabled():
                    persistent_store.replace_scanner_results(buckets)
                else:
                    pipe = r.pipeline()
                    pipe.delete(H_SCAN_RESULTS)
                    for bucket in WATCHLISTS:
                        key = f"{H_SCAN_PREFIX}{bucket}"
                        pipe.delete(key)
                        for sym, row in buckets[bucket].items():
                            payload = json.dumps(row)
                            pipe.hset(key, sym, payload)
                            if bucket in ("priority", "actionable"):
                                pipe.hset(H_SCAN_RESULTS, sym, payload)
                    pipe.execute()
                _audit_volume_crossings(universe_quotes, tickers, buckets)
                result_count = sum(len(rows) for rows in buckets.values())
                status = "ok" if failed_count == 0 and not quote_errors and not discovery_errors else "degraded"
                if tickers and result_count == 0:
                    status = "error"
                self._heartbeat(
                    status,
                    started_ts=started,
                    market_data_source="schwab",
                    candidate_count=len(all_tickers),
                    ticker_count=len(tickers),
                    result_count=result_count,
                    failed_count=failed_count,
                    error_samples=error_samples,
                    news_lookback_days=SCANNER_NEWS_LOOKBACK_DAYS,
                    rotation_mode="automatic_price_bounded_catalysts",
                    discovery_count=len(discovery_tickers),
                    price_eligible_count=len(all_tickers),
                    price_excluded_count=sum(
                        1 for symbol in discovery_tickers
                        if symbol in universe_quotes and symbol not in eligible
                    ),
                    quote_missing_count=sum(
                        1 for symbol in discovery_tickers if symbol not in universe_quotes
                    ),
                    source_counts={
                        source: sum(source in values for values in watch_sources.values())
                        for source in sorted({value for values in watch_sources.values() for value in values})
                    },
                    quote_monitored_count=len(universe_quotes),
                    quote_batch_count=(
                        (len(discovery_tickers) + SCANNER_QUOTE_BATCH_SIZE - 1)
                        // SCANNER_QUOTE_BATCH_SIZE
                    ),
                    quote_errors=quote_errors,
                    discovery_errors=discovery_errors,
                    mover_priority_count=min(
                        len(mover_symbols), SCANNER_MOVER_SYMBOLS_PER_CYCLE
                    ),
                    volume_trigger_count=len(volume_triggers),
                    volume_trigger_selected_count=sum(symbol in tickers for symbol in volume_triggers),
                    volume_trigger_symbols=volume_triggers[:20],
                    quote_rvol_estimate_count=sum("rvol_proxy" in q for q in universe_quotes.values()),
                    cycle_duration_seconds=round(time.monotonic() - cycle_started_monotonic, 2),
                    company_metadata_refresh=_COMPANY_CACHE.status(),
                    rotation_reserved_slots=(max(1, SCANNER_MAX_TICKERS_PER_CYCLE // 2)
                                             if len(all_tickers) > SCANNER_MAX_TICKERS_PER_CYCLE else 0),
                    estimated_full_sweep_cycles=(
                        (len(all_tickers) + max(1, SCANNER_MAX_TICKERS_PER_CYCLE // 2) - 1)
                        // max(1, SCANNER_MAX_TICKERS_PER_CYCLE // 2)
                        if len(all_tickers) > SCANNER_MAX_TICKERS_PER_CYCLE else 1
                    ),
                    estimated_full_sweep_minutes=round(
                        (
                            (len(all_tickers) + max(1, SCANNER_MAX_TICKERS_PER_CYCLE // 2) - 1)
                            // max(1, SCANNER_MAX_TICKERS_PER_CYCLE // 2)
                            if len(all_tickers) > SCANNER_MAX_TICKERS_PER_CYCLE else 1
                        ) * max(1, settings.get("scan_interval", SCAN_INTERVAL),
                                time.monotonic() - cycle_started_monotonic + 1) / 60,
                        1,
                    ),
                    watchlist_counts={name: len(buckets[name]) for name in WATCHLISTS},
                )
                if failed_count:
                    logger.warning(
                        "Schwab scanner cycle completed with failures: scanned=%s results=%s failed=%s samples=%s",
                        len(tickers), result_count, failed_count, error_samples,
                    )
            except Exception as e:
                logger.exception("Scanner worker cycle failed")
                self._heartbeat("error", error=str(e))
            finally:
                interval = max(1, _settings().get("scan_interval", SCAN_INTERVAL))
                elapsed = time.monotonic() - cycle_started_monotonic
                time.sleep(max(1, interval - elapsed))

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting scanner worker")
    worker = ScannerWorker()
    worker.run_forever()

