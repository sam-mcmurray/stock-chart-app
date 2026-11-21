import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import redis
import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from drl_agent.environment import StockTradingEnv
from workers.scanner_worker import (
    H_SCAN_PREFIX,
    H_SCAN_SETTINGS,
    H_SCAN_HEARTBEAT,
    WATCHLISTS,
    _avg_volume_ratio,
    _company_info,
    _get_daily,
    _get_intraday,
    _latest_quote,
    _macd_open,
    _news_watch_summary,
    _recent_articles,
    _settings,
    _watch_source_details,
)
from workers.news_worker import (
    H_NEWS_HEARTBEAT,
    GDELT_BACKFILL_CHUNK_HOURS,
    GDELT_BACKFILL_DAYS,
    GDELT_BACKFILL_ENABLED,
    GDELT_BACKFILL_MAX_CHUNKS,
    GDELT_BACKFILL_MAX_RECORDS,
    GDELT_BACKFILL_MIN_INTERVAL_SECONDS,
    GDELT_ENABLED,
    GDELT_MAX_RECORDS,
    GDELT_MIN_INTERVAL_SECONDS,
    GDELT_QUERY,
    GDELT_TIMESPAN,
    GDELT_SORT,
    GDELT_EXPORT_ENABLED,
    GDELT_EXPORT_CATCHUP_MAX_BATCHES,
    GDELT_EXPORT_LAG_ALERT_BATCHES,
    GDELT_EXPORT_MIN_INTERVAL_SECONDS,
    FINNHUB_NEWS_ENABLED,
    FINNHUB_NEWS_MIN_INTERVAL_SECONDS,
    YAHOO_RSS_ENABLED,
    MARKETWATCH_RSS_ENABLED,
    THESTREET_RSS_ENABLED,
    NEWS_RSS_MIN_INTERVAL_SECONDS,
    K_FINNHUB_LAST_FETCH,
    K_YAHOO_RSS_LAST_FETCH,
    K_MARKETWATCH_RSS_LAST_FETCH,
    K_THESTREET_RSS_LAST_FETCH,
    K_RSS_VERIFICATION_SCORES,
    K_GDELT_EXPORT_LAST_CHECK,
    K_GDELT_EXPORT_LAST_URL,
    K_GDELT_EXPORT_BACKLOG,
    K_GDELT_EXPORT_LATEST_URL,
    K_GDELT_EXPORT_LAG_BATCHES,
    K_GDELT_BACKFILL_CURSOR,
    K_GDELT_BACKFILL_LAST_RUN,
    K_GDELT_LAST_FETCH,
    K_NEWSAPI_LAST_FETCH,
    K_NEWSAPI_RATE_LIMITED,
    K_NEWS_DEDUP_ORIGINAL_COUNT,
    K_NEWS_DEDUP_CONSOLIDATED_COUNT,
    K_NEWS_VECTOR_DEDUP_ORIGINAL_COUNT,
    K_NEWS_VECTOR_DEDUP_CONSOLIDATED_COUNT,
    K_NEWS_GRAPH_DEDUP_REMOVED_COUNT,
    NEWSAPI_DAILY_REQUEST_LIMIT,
    NEWSAPI_ENABLED,
    NEWSAPI_BACKFILL_DAYS,
    NEWSAPI_BACKFILL_MAX_PAGES,
    NEWSAPI_BACKFILL_QUERY,
    NEWSAPI_MIN_INTERVAL_SECONDS,
    _newsapi_daily_key,
)
from services.decision_engine import DecisionConfig, decide
from services.calendar_event_extractor import news_article_calendar_event
from services.calendar_relevance import (
    configured as calendar_llm_configured,
    display_topics,
    normalize_commodities,
)
from services.calendar_relevance_queue import (
    CLASSIFIED_KEY as CALENDAR_RELEVANCE_CLASSIFIED_KEY,
    ENRICHMENTS_KEY as NEWS_LLM_ENRICHMENTS_KEY,
    FAILED_KEY as CALENDAR_RELEVANCE_FAILED_KEY,
    HEARTBEAT_KEY as CALENDAR_RELEVANCE_HEARTBEAT_KEY,
    pending_counts as calendar_relevance_pending_counts,
)
from services.financial_article_analysis import analyze_candidate_articles, apply_analysis_guardrail, should_analyze_candidate
from services.decision_history import list_decisions, record_decision
from services import persistent_store
from services.gdelt_event_queue import PENDING_KEY as GDELT_EVENT_PENDING_KEY
from services.gdelt_event_queue import PROCESSING_KEY as GDELT_EVENT_PROCESSING_KEY
from services.gdelt_event_queue import DEAD_LETTER_KEY as GDELT_EVENT_DEAD_LETTER_KEY
from routers.auth import require_access_token
from schwab.market_data import SchwabMarketDataClient
from schwab.user_preferences import UserPreferences

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)
_PRICE_CACHE = {}
_PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL_SECONDS", "300"))
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "yfinance").lower()
PRICE_KEY_TEMPLATE = os.getenv("PRICE_KEY_TEMPLATE", "price:{symbol}")
_AGENT_CACHE = None
_RECOMMENDATION_CACHE = {}
_RECOMMENDATION_TTL = int(os.getenv("AGENT_RECO_TTL_SECONDS", "30"))
NEWS_EVENT_LLM_ENABLED = os.getenv("NEWS_EVENT_LLM_ENABLED", "true").lower() == "true"
NEWS_EVENT_LLM_LIMIT = int(os.getenv("NEWS_EVENT_LLM_LIMIT", "20"))
NEWS_EVENT_LLM_TIMEOUT = float(os.getenv("NEWS_EVENT_LLM_TIMEOUT_SECONDS", "8"))
GDELT_EVENT_COLLECTION_NAME = os.getenv("QDRANT_GDELT_EVENT_COLLECTION", "gdelt_events")
try:
    MARKET_CALENDAR_TIMEZONE = ZoneInfo(os.getenv("MARKET_CALENDAR_TIMEZONE", "America/Chicago"))
except Exception:
    MARKET_CALENDAR_TIMEZONE = timezone.utc

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


class NewsQualityReview(BaseModel):
    status: str = Field(pattern="^(reviewed|dismissed)$")
    note: str = Field(default="", max_length=1000)
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

router = APIRouter(tags=["scanner"])

class ScannerSettingsUpdate(BaseModel):
    price_min: float | None = Field(None, ge=0)
    price_max: float | None = Field(None, gt=0)
    rvol_threshold: float | None = Field(None, ge=0)
    rvol_window_days: int | None = Field(None, ge=20, le=252)
    float_max: int | None = Field(None, ge=1)
    scan_interval: int | None = Field(None, ge=5)

def _rows_from_hash(key: str):
    try:
        if key == "scanner:focus" and persistent_store.enabled():
            return persistent_store.list_scanner_results(focus_only=True)
        if key.startswith(H_SCAN_PREFIX) and persistent_store.enabled():
            return persistent_store.list_scanner_results(bucket=key.removeprefix(H_SCAN_PREFIX))
    except Exception:
        pass

    rows = r.hgetall(key) or {}
    out = []
    for raw in rows.values():
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out

def _hash_count(key: str) -> int:
    try:
        if key == "scanner:focus" and persistent_store.enabled():
            return persistent_store.count_scanner_results(focus_only=True)
        if key.startswith(H_SCAN_PREFIX) and persistent_store.enabled():
            return persistent_store.count_scanner_results(bucket=key.removeprefix(H_SCAN_PREFIX))
    except Exception:
        pass

    try:
        return int(r.hlen(key) or 0)
    except Exception:
        return len(r.hgetall(key) or {})

def _zset_count(key: str) -> int:
    try:
        return int(r.zcard(key) or 0)
    except Exception:
        try:
            return len(r.zrange(key, 0, -1) or [])
        except Exception:
            return 0


def _scanner_discovery_sources(symbol: str) -> list[str]:
    normalized = str(symbol or "").strip().upper()
    try:
        if persistent_store.enabled():
            rows = persistent_store.list_scanner_results()
            row = next(
                (item for item in rows if str(item.get("symbol") or "").upper() == normalized),
                None,
            )
            return list((row or {}).get("watch_sources") or [])
    except Exception:
        pass
    for bucket in WATCHLISTS:
        raw = r.hget(f"{H_SCAN_PREFIX}{bucket}", normalized)
        if not raw:
            continue
        try:
            return list((json.loads(raw) or {}).get("watch_sources") or [])
        except (TypeError, ValueError):
            return []
    return []


def _attach_discovery_sources(payload: dict, symbol: str) -> None:
    sources = _scanner_discovery_sources(symbol)
    if not sources:
        sources = ["direct_lookup"]
    details = _watch_source_details(sources)
    payload["watch_sources"] = sources
    payload["watch_source_details"] = details
    if isinstance(payload.get("catalyst"), dict):
        payload["catalyst"]["sources"] = details

def _json_value(key: str):
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None

def _heartbeat_age(heartbeat: dict | None) -> int | None:
    if not isinstance(heartbeat, dict) or not heartbeat.get("ts"):
        return None
    try:
        return max(0, int(time.time()) - int(heartbeat["ts"]))
    except Exception:
        return None

def _int_value(key: str) -> int:
    try:
        return int(r.get(key) or 0)
    except Exception:
        return 0


def _decode_news_text(value):
    if not isinstance(value, str):
        return value
    return html.unescape(html.unescape(value))


def _recent_news_articles(
    limit: int = 100,
    max_age_hours: int | None = None,
    sort: str = "newest",
    offset: int = 0,
    *,
    display_feed: bool = True,
):
    descending = sort != "oldest"
    offset = max(0, int(offset))
    if max_age_hours is None:
        ids = (
            r.zrevrange("news:recent:articles", offset, offset + max(limit - 1, 0))
            if descending
            else r.zrange("news:recent:articles", offset, offset + max(limit - 1, 0))
        )
    else:
        cutoff = int(time.time()) - max(1, max_age_hours) * 60 * 60
        if descending:
            ids = r.zrevrangebyscore(
                "news:recent:articles", "+inf", cutoff, start=offset, num=max(1, limit)
            )
        else:
            ids = r.zrangebyscore(
                "news:recent:articles", cutoff, "+inf", start=offset, num=max(1, limit)
            )
    enrichment_rows = r.hmget(NEWS_LLM_ENRICHMENTS_KEY, ids) if ids else []
    if not isinstance(enrichment_rows, (list, tuple)):
        enrichment_rows = []
    enrichments = {}
    for aid, raw in zip(ids, enrichment_rows):
        try:
            enrichments[aid] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            enrichments[aid] = {}
    out = []
    for aid in ids:
        raw = r.hget(f"news:article:{aid}", "json")
        if raw:
            try:
                article = json.loads(raw)
                enrichment = enrichments.get(aid) or {}
                article["original_tickers"] = article.get("tickers") or []
                article["tickers"] = []
                article["ticker_source"] = "pending_llm_validation"
                if enrichment:
                    original_title = article.get("title") or ""
                    article["original_title"] = original_title
                    article["title"] = enrichment.get("display_headline") or original_title
                    article["display_headline"] = article["title"]
                    article["headline_quality"] = enrichment.get("headline_quality")
                    article["headline_source"] = enrichment.get("headline_source")
                    article["headline_verified"] = enrichment.get("headline_verified")
                    article["company_identification_verified"] = enrichment.get("company_identification_verified")
                    article["resolved_article_url"] = enrichment.get("resolved_article_url")
                    article["llm_summary"] = enrichment.get("llm_summary")
                    article["related_commodities"] = normalize_commodities(
                        enrichment.get("related_commodities"),
                        enrichment.get("llm_topics"),
                    )
                    article["llm_topics"] = display_topics(
                        enrichment.get("llm_topics"),
                        article["related_commodities"],
                    )
                    article["relevance_label"] = enrichment.get("relevance_label")
                    article["relevance_confidence"] = enrichment.get("relevance_confidence")
                    article["related_companies"] = enrichment.get("related_companies") or []
                    article["company_relationships"] = enrichment.get("company_relationships") or []
                    article["source_language"] = enrichment.get("source_language") or "English"
                    article["translated_to_english"] = bool(enrichment.get("translated_to_english"))
                    article["tickers"] = sorted(set(enrichment.get("related_tickers") or []))
                    article["ticker_source"] = "llm_company_validated"
                for field in ("title", "display_headline", "original_title", "summary", "llm_summary", "author", "source"):
                    if field in article:
                        article[field] = _decode_news_text(article[field])
                out.append(article)
            except Exception:
                continue
    return out

def _article_text(article):
    return f"{article.get('title') or ''} {article.get('summary') or article.get('description') or ''}"

def _event_type(article):
    text = _article_text(article).lower()
    if re.search(r"\b(ipo|initial public offering|public debut|direct listing|spac)\b", text):
        return "IPO watch"
    if re.search(r"\b(cpi|ppi|jobs report|nonfarm|payrolls|fed|fomc|inflation|rates|gdp|pce)\b", text):
        return "Economic release"
    return "Market note"

def _published_dt(article):
    try:
        return datetime.fromtimestamp(int(article.get("published_ts") or time.time()), tz=MARKET_CALENDAR_TIMEZONE)
    except Exception:
        return datetime.now(MARKET_CALENDAR_TIMEZONE)

def _date_key(dt):
    return dt.date().isoformat()

def _infer_year_month_day(month, day, article):
    base = _published_dt(article)
    candidate = datetime(base.year, month, day, tzinfo=MARKET_CALENDAR_TIMEZONE)
    if (base - candidate).days > 180:
        candidate = datetime(base.year + 1, month, day, tzinfo=MARKET_CALENDAR_TIMEZONE)
    return candidate

def _infer_weekday_date(weekday, article):
    base = _published_dt(article)
    delta = (weekday - base.weekday()) % 7 or 7
    return base + timedelta(days=delta)

def _infer_event_date(article):
    text = _article_text(article)
    iso = re.search(r"\b(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b", text)
    if iso:
      return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"

    month_day = re.search(
        r"\b(?:on|for|due|scheduled|reports?|reporting|release(?:s|d)?|meeting|starts?|opens?|launch(?:es|ed)?)\s+(?:on\s+)?"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
        r"([0-3]?\d)(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\b",
        text,
        re.I,
    )
    if month_day:
        month = _MONTHS[month_day.group(1).lower().replace(".", "")]
        day = int(month_day.group(2))
        year = int(month_day.group(3)) if month_day.group(3) else None
        dt = datetime(year, month, day, tzinfo=MARKET_CALENDAR_TIMEZONE) if year else _infer_year_month_day(month, day, article)
        return _date_key(dt)

    weekday = re.search(r"\b(?:on|due|scheduled|reports?|reporting|meeting|starts?|opens?|launch(?:es|ed)?)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, re.I)
    if weekday:
        return _date_key(_infer_weekday_date(_WEEKDAYS[weekday.group(1).lower()], article))

    return _date_key(_published_dt(article))

def _deterministic_news_event(article):
    return news_article_calendar_event(article)

def _llm_event_overrides(articles):
    try:
        from routers import rag_api
    except Exception:
        return []

    if not NEWS_EVENT_LLM_ENABLED or rag_api.RAG_LLM_PROVIDER not in ("openai", "openai-compatible", "ollama"):
        return []

    try:
        import openai
    except Exception:
        return []

    base = rag_api.LLM_API_BASE
    if not base and rag_api.RAG_LLM_PROVIDER == "ollama":
        base = rag_api.DEFAULT_OLLAMA_BASE
    client_kwargs = {}
    if base:
        client_kwargs["base_url"] = base
    client = openai.OpenAI(api_key=rag_api.LLM_API_KEY or None, timeout=NEWS_EVENT_LLM_TIMEOUT, **client_kwargs)
    payload = [
        {
            "id": str(index),
            "title": article.get("title") or "",
            "summary": article.get("summary") or "",
            "published_date": _date_key(_published_dt(article)),
            "source": article.get("source") or "",
            "url": article.get("url") or "",
            "tickers": article.get("tickers") or [],
        }
        for index, article in enumerate(articles[:NEWS_EVENT_LLM_LIMIT])
    ]
    prompt = (
        "Extract market calendar events from these articles. Return only JSON with an events array. "
        "Each event must include article_id, date YYYY-MM-DD, type one of IPO watch, Earnings watch, "
        "Economic release, Market note, title, symbols array, and confidence 0-1. Use the actual event "
        "date if mentioned; otherwise use published_date.\n\n"
        + json.dumps(payload)[:12000]
    )
    try:
        response = client.chat.completions.create(
            model=rag_api.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        return parsed.get("events") if isinstance(parsed.get("events"), list) else []
    except Exception:
        return []

def _merge_llm_events(base_events, llm_events):
    merged = list(base_events)
    for event in llm_events:
        try:
            index = int(event.get("article_id"))
            if index < 0 or index >= len(merged):
                continue
            base = merged[index]
            if re.match(r"^20\d{2}-\d{2}-\d{2}$", str(event.get("date") or "")):
                base["date"] = event["date"]
            if event.get("type") in {"IPO watch", "Earnings watch", "Economic release", "Market note"}:
                base["type"] = event["type"]
            if isinstance(event.get("symbols"), list):
                base["symbols"] = [str(s).upper() for s in event["symbols"] if s]
            if isinstance(event.get("confidence"), (int, float)):
                base["confidence"] = max(base["confidence"], min(float(event["confidence"]), 1.0))
            base["extraction_source"] = "llm"
        except Exception:
            continue
    return merged

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    cursor = datetime(year, month, 1, tzinfo=MARKET_CALENDAR_TIMEZONE)
    delta = (weekday - cursor.weekday()) % 7
    return cursor + timedelta(days=delta + (n - 1) * 7)

def _last_business_day(year: int, month: int) -> datetime:
    if month == 12:
        cursor = datetime(year + 1, 1, 1, tzinfo=MARKET_CALENDAR_TIMEZONE) - timedelta(days=1)
    else:
        cursor = datetime(year, month + 1, 1, tzinfo=MARKET_CALENDAR_TIMEZONE) - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor

def _generated_calendar_events(days_back: int = 14, days_ahead: int = 60):
    today = datetime.now(MARKET_CALENDAR_TIMEZONE).date()
    start = today - timedelta(days=max(0, days_back))
    end = today + timedelta(days=max(0, days_ahead))
    events = []
    cursor = datetime(start.year, start.month, 1, tzinfo=MARKET_CALENDAR_TIMEZONE)
    end_cursor = datetime(end.year, end.month, 1, tzinfo=MARKET_CALENDAR_TIMEZONE)

    while cursor <= end_cursor:
        year = cursor.year
        month = cursor.month
        candidates = [
            (_nth_weekday(year, month, 4, 1), "Economic release", "Jobs report watch", "Estimated first-Friday jobs-report window"),
            (_nth_weekday(year, month, 2, 2), "Economic release", "CPI watch", "Estimated second-Wednesday CPI window"),
            (_nth_weekday(year, month, 4, 3), "Market note", "Monthly options expiration", "Third-Friday monthly options expiration"),
            (_last_business_day(year, month), "Market note", "Month-end positioning", "Last-business-day month-end watch"),
        ]
        for dt, event_type, title, source in candidates:
            event_date = dt.date()
            if start <= event_date <= end:
                key = _date_key(dt)
                events.append({
                    "id": f"generated:{title.lower().replace(' ', '-')}:{key}",
                    "date": key,
                    "type": event_type,
                    "title": title,
                    "source": source,
                    "url": None,
                    "symbols": [],
                    "confidence": 0.35,
                    "extraction_source": "generated",
                })
        cursor = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1, tzinfo=MARKET_CALENDAR_TIMEZONE)

    return events

def _mk_schwab_market_client(access_token: str) -> SchwabMarketDataClient:
    prefs = UserPreferences(access_token=access_token)
    return SchwabMarketDataClient(user_prefs=prefs, access_token=access_token)

def _quote_number(payload: dict, *keys: str) -> float | None:
    for key in keys:
        try:
            value = float(payload.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None

@router.get("/scanner/focus")
def scanner_focus():
    rows = _rows_from_hash("scanner:focus")
    return JSONResponse(sorted(rows, key=lambda x: x.get("score", 0), reverse=True))

@router.get("/scanner/high-rvol")
def scanner_high_rvol(min_rvol: float | None = None, limit: int = 100):
    settings = _settings()
    threshold = float(min_rvol if min_rvol is not None else settings["rvol_threshold"])
    rows = _rows_from_hash("scanner:focus")
    filtered = [
        row for row in rows
        if float(row.get("rvol") or row.get("avr") or 0.0) >= threshold
    ]
    filtered.sort(key=lambda x: float(x.get("rvol") or x.get("avr") or 0.0), reverse=True)
    return JSONResponse({
        "min_rvol": threshold,
        "count": len(filtered),
        "items": filtered[: max(1, min(int(limit), 500))],
    })

@router.get("/scanner/settings")
def scanner_settings():
    return JSONResponse(_settings())

@router.get("/scanner/status")
def scanner_status():
    watchlist_counts = {
        bucket: _hash_count(f"{H_SCAN_PREFIX}{bucket}")
        for bucket in WATCHLISTS
    }
    focus_count = _hash_count("scanner:focus")
    recent_ticker_count = _zset_count("news:recent:tickers")
    heartbeat = _json_value(H_SCAN_HEARTBEAT)
    return JSONResponse({
        "ok": True,
        "settings": _settings(),
        "heartbeat": heartbeat,
        "heartbeat_age_seconds": _heartbeat_age(heartbeat),
        "focus_count": focus_count,
        "watchlist_counts": watchlist_counts,
        "recent_ticker_count": recent_ticker_count,
        "backlog": {
            "recent_tickers": recent_ticker_count,
            "focus_results": focus_count,
            "watchlists_total": sum(watchlist_counts.values()),
        },
    })

@router.put("/scanner/settings")
def update_scanner_settings(body: ScannerSettingsUpdate):
    updates = body.dict(exclude_none=True)
    if not updates:
        return JSONResponse(_settings())
    price_min = updates.get("price_min")
    price_max = updates.get("price_max")
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=400, detail="price_min must be less than or equal to price_max")
    if persistent_store.enabled():
        persistent_store.save_scanner_settings(updates, _settings())
    else:
        r.hset(H_SCAN_SETTINGS, mapping={k: str(v) for k, v in updates.items()})
    return JSONResponse(_settings())

@router.get("/scanner/watchlists")
def scanner_watchlists():
    payload = {}
    for bucket in WATCHLISTS:
        rows = _rows_from_hash(f"{H_SCAN_PREFIX}{bucket}")
        payload[bucket] = sorted(rows, key=lambda x: x.get("score", x.get("rvol", 0)), reverse=True)
    return JSONResponse(payload)

@router.get("/scanner/watchlists/{name}")
def scanner_watchlist(name: str):
    if name not in WATCHLISTS:
        raise HTTPException(status_code=404, detail=f"Unknown watchlist: {name}")
    rows = _rows_from_hash(f"{H_SCAN_PREFIX}{name}")
    return JSONResponse(sorted(rows, key=lambda x: x.get("score", x.get("rvol", 0)), reverse=True))

@router.get("/scanner/schwab/quotes")
def scanner_schwab_quotes(
    symbols: str = Query(..., min_length=1, max_length=1500),
    access_token: str = Depends(require_access_token),
):
    requested = list(dict.fromkeys(
        symbol.strip().upper()
        for symbol in symbols.split(",")
        if symbol.strip()
    ))
    if not requested or len(requested) > 100:
        raise HTTPException(status_code=400, detail="Request between 1 and 100 symbols")
    invalid = [symbol for symbol in requested if not re.fullmatch(r"[A-Z0-9.$/-]{1,15}", symbol)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid symbols: {', '.join(invalid)}")

    try:
        raw_quotes = _mk_schwab_market_client(access_token).get_quotes(
            requested, fields="quote,reference"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Schwab quote request failed: {exc}") from exc

    fetched_ts = int(time.time())
    quotes = {}
    for symbol in requested:
        instrument = raw_quotes.get(symbol) if isinstance(raw_quotes, dict) else None
        if not isinstance(instrument, dict):
            continue
        quote = instrument.get("quote") if isinstance(instrument.get("quote"), dict) else {}
        reference = instrument.get("reference") if isinstance(instrument.get("reference"), dict) else {}
        last = _quote_number(quote, "lastPrice", "mark", "closePrice")
        if last is None:
            continue
        quote_time_ms = quote.get("quoteTime") or quote.get("tradeTime")
        try:
            market_ts = int(quote_time_ms) // 1000 if quote_time_ms else None
        except (TypeError, ValueError):
            market_ts = None
        quotes[symbol] = {
            "symbol": symbol,
            "description": reference.get("description") or "",
            "last": last,
            "bid": _quote_number(quote, "bidPrice"),
            "ask": _quote_number(quote, "askPrice"),
            "mark": _quote_number(quote, "mark"),
            "net_change": _quote_number(quote, "netChange", "regularMarketNetChange"),
            "day_change_pct": _quote_number(
                quote, "netPercentChange", "regularMarketPercentChange"
            ),
            "previous_close": _quote_number(quote, "closePrice"),
            "market_ts": market_ts,
            "fetched_ts": fetched_ts,
            "realtime": bool(instrument.get("realtime", True)),
            "source": "schwab",
        }

    return JSONResponse({
        "quotes": quotes,
        "requested_count": len(requested),
        "quote_count": len(quotes),
        "missing_symbols": [symbol for symbol in requested if symbol not in quotes],
        "fetched_ts": fetched_ts,
    })


@router.get("/scanner/context/{symbol}")
def scanner_context(
    symbol: str,
    _access_token: str = Depends(require_access_token),
):
    normalized = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.$/-]{1,15}", normalized):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    articles = _recent_articles(normalized, limit=20)
    return JSONResponse({
        "symbol": normalized,
        "company": _company_info(normalized),
        "articles": articles,
        "news_watch": _news_watch_summary(articles),
    })

@router.get("/scanner/decision/{symbol}")
def scanner_decision(symbol: str):
    settings = _settings()
    df = _get_daily(symbol.upper())
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No price history available for {symbol.upper()}")
    price = _latest_quote(symbol.upper(), df)
    rvol = _avg_volume_ratio(df, settings["rvol_window_days"])
    company = _company_info(symbol.upper())
    macd_open = _macd_open(df)
    articles = _recent_articles(symbol.upper(), limit=10)
    intraday = _get_intraday(symbol.upper())
    payload = decide(
        symbol.upper(),
        df,
        price=price,
        rvol=rvol,
        float_shares=company.get("float_shares"),
        macd_open=macd_open,
        articles=articles,
        intraday_df=intraday,
        config=DecisionConfig(
            min_rvol=settings["rvol_threshold"],
            max_float=settings["float_max"],
        ),
    )
    if should_analyze_candidate(payload):
        apply_analysis_guardrail(payload, analyze_candidate_articles(symbol.upper(), articles))
    payload["company"] = company
    payload["articles"] = articles
    payload["news_watch"] = _news_watch_summary(articles)
    _attach_discovery_sources(payload, symbol.upper())
    return JSONResponse(payload)

@router.get("/scanner/schwab/decision/{symbol}")
def scanner_schwab_decision(
    symbol: str,
    access_token: str = Depends(require_access_token),
):
    """
    Authenticated decision endpoint using Schwab market data first.
    Falls back to existing public sources only when Schwab quote/history is unavailable.
    """
    sym = symbol.upper()
    settings = _settings()
    source = "schwab"
    try:
        client = _mk_schwab_market_client(access_token)
        daily = client.get_daily_frame(sym, months=3)
        intraday = client.get_intraday_frame(sym, days=2, frequency=5)
        price = client.get_last_price(sym)
    except Exception as exc:
        source = f"fallback:{exc}"
        daily = _get_daily(sym)
        intraday = _get_intraday(sym)
        price = _latest_quote(sym, daily) if daily is not None else 0.0

    if daily is None or daily.empty:
        raise HTTPException(status_code=404, detail=f"No Schwab or fallback price history available for {sym}")

    rvol = _avg_volume_ratio(daily, settings["rvol_window_days"])
    company = _company_info(sym)
    macd_open = _macd_open(daily)
    articles = _recent_articles(sym, limit=10)
    payload = decide(
        sym,
        daily,
        price=price,
        rvol=rvol,
        float_shares=company.get("float_shares"),
        macd_open=macd_open,
        articles=articles,
        intraday_df=intraday,
        config=DecisionConfig(
            min_rvol=settings["rvol_threshold"],
            max_float=settings["float_max"],
        ),
    )
    if should_analyze_candidate(payload):
        apply_analysis_guardrail(payload, analyze_candidate_articles(sym, articles))
    payload["company"] = company
    payload["articles"] = articles
    payload["news_watch"] = _news_watch_summary(articles)
    _attach_discovery_sources(payload, sym)
    payload["data_source"] = source
    try:
        record_decision(sym, payload, source="schwab_detail")
    except Exception:
        pass
    return JSONResponse(payload)

@router.get("/scanner/decisions/history")
def scanner_decision_history(
    symbol: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    return JSONResponse(
        jsonable_encoder(
            list_decisions(symbol=symbol, limit=limit, offset=offset)
        )
    )

@router.get("/news/recent")
def news_recent(
    limit: int = 100,
    max_age_hours: int | None = None,
    sort: str = "newest",
    offset: int = 0,
):
    if sort not in {"newest", "oldest"}:
        raise HTTPException(status_code=422, detail="sort must be newest or oldest")
    return JSONResponse(_recent_news_articles(
        min(max(1, limit), 500),
        max_age_hours,
        sort,
        max(0, offset),
    ))

@router.get("/news/events")
def news_events(
    limit: int = 100,
    include_generated: bool = True,
    generated_days_back: int = 14,
    generated_days_ahead: int = 60,
    start_date: str | None = Query(None, pattern=r"^20\d{2}-\d{2}-\d{2}$"),
    end_date: str | None = Query(None, pattern=r"^20\d{2}-\d{2}-\d{2}$"),
):
    articles = [
        article for article in _recent_news_articles(limit, display_feed=False)
        if article.get("relevance_label") in {"finance", "business", "markets"}
    ]
    base_events = [_deterministic_news_event(article) for article in articles]
    llm_events = _llm_event_overrides(articles)
    events = [
        event for event in _merge_llm_events(base_events, llm_events)
        if event.get("type") != "IPO watch"
    ]
    if persistent_store.enabled():
        persistent_store.upsert_calendar_events(events)
        events = persistent_store.list_calendar_events(
            limit=10000,
            start_date=start_date,
            end_date=end_date,
        )
    events = [
        event for event in events
        if event.get("details", {}).get("calendar_kind") != "market_news"
        or event.get("details", {}).get("relevance_label") in {"finance", "business", "markets"}
    ]
    if include_generated:
        events.extend(_generated_calendar_events(generated_days_back, generated_days_ahead))
    unique = {event.get("id"): event for event in events if event.get("id")}
    return JSONResponse(list(unique.values()))

@router.get("/news/status")
def news_status():
    heartbeat = _json_value(H_NEWS_HEARTBEAT)
    recent_article_count = _zset_count("news:recent:articles")
    recent_ticker_count = _zset_count("news:recent:tickers")
    newsapi_last_fetch = _int_value(K_NEWSAPI_LAST_FETCH)
    newsapi_daily_count = _int_value(_newsapi_daily_key())
    newsapi_rate_limited = _int_value(K_NEWSAPI_RATE_LIMITED)
    gdelt_last_fetch = _int_value(K_GDELT_LAST_FETCH)
    gdelt_backfill_last_run = _int_value(K_GDELT_BACKFILL_LAST_RUN)
    gdelt_backfill_cursor = _int_value(K_GDELT_BACKFILL_CURSOR)
    finnhub_last_fetch = _int_value(K_FINNHUB_LAST_FETCH)
    yahoo_rss_last_fetch = _int_value(K_YAHOO_RSS_LAST_FETCH)
    marketwatch_rss_last_fetch = _int_value(K_MARKETWATCH_RSS_LAST_FETCH)
    thestreet_rss_last_fetch = _int_value(K_THESTREET_RSS_LAST_FETCH)
    gdelt_export_last_check = _int_value(K_GDELT_EXPORT_LAST_CHECK)
    gdelt_export_last_url = r.get(K_GDELT_EXPORT_LAST_URL)
    gdelt_export_latest_url = r.get(K_GDELT_EXPORT_LATEST_URL)
    gdelt_export_lag_batches = _int_value(K_GDELT_EXPORT_LAG_BATCHES)
    gdelt_export_backlog = bool(_int_value(K_GDELT_EXPORT_BACKLOG))
    gdelt_event_pending = int(r.llen(GDELT_EVENT_PENDING_KEY) or 0)
    gdelt_event_processing = int(r.llen(GDELT_EVENT_PROCESSING_KEY) or 0)
    gdelt_event_dead_letter = int(r.llen(GDELT_EVENT_DEAD_LETTER_KEY) or 0)
    dedup_original_count = _int_value(K_NEWS_DEDUP_ORIGINAL_COUNT)
    dedup_consolidated_count = _int_value(K_NEWS_DEDUP_CONSOLIDATED_COUNT)
    vector_dedup_original_count = _int_value(K_NEWS_VECTOR_DEDUP_ORIGINAL_COUNT)
    vector_dedup_consolidated_count = _int_value(K_NEWS_VECTOR_DEDUP_CONSOLIDATED_COUNT)
    graph_dedup_removed_count = _int_value(K_NEWS_GRAPH_DEDUP_REMOVED_COUNT)
    relevance_pending = calendar_relevance_pending_counts(r)
    return JSONResponse({
        "ok": True,
        "heartbeat": heartbeat,
        "heartbeat_age_seconds": _heartbeat_age(heartbeat),
        "recent_article_count": recent_article_count,
        "recent_ticker_count": recent_ticker_count,
        "deduplication": {
            "original_count": dedup_original_count,
            "consolidated_count": dedup_consolidated_count,
            "duplicates_removed": max(0, dedup_original_count - dedup_consolidated_count),
            "vector_original_count": vector_dedup_original_count,
            "vector_consolidated_count": vector_dedup_consolidated_count,
            "vector_duplicates_removed": max(0, vector_dedup_original_count - vector_dedup_consolidated_count),
            "graph_nodes_removed": graph_dedup_removed_count,
        },
        "newsapi": {
            "enabled": NEWSAPI_ENABLED,
            "daily_count": newsapi_daily_count,
            "daily_limit": NEWSAPI_DAILY_REQUEST_LIMIT,
            "min_interval_seconds": NEWSAPI_MIN_INTERVAL_SECONDS,
            "backfill_days": NEWSAPI_BACKFILL_DAYS,
            "backfill_query": NEWSAPI_BACKFILL_QUERY,
            "backfill_max_pages": NEWSAPI_BACKFILL_MAX_PAGES,
            "last_fetch_ts": newsapi_last_fetch or None,
            "rate_limited_ts": newsapi_rate_limited or None,
        },
        "finnhub": {
            "enabled": FINNHUB_NEWS_ENABLED,
            "min_interval_seconds": FINNHUB_NEWS_MIN_INTERVAL_SECONDS,
            "last_fetch_ts": finnhub_last_fetch or None,
        },
        "rss": {
            "min_interval_seconds": NEWS_RSS_MIN_INTERVAL_SECONDS,
            "yahoo": {"enabled": YAHOO_RSS_ENABLED, "last_fetch_ts": yahoo_rss_last_fetch or None},
            "marketwatch": {"enabled": MARKETWATCH_RSS_ENABLED, "last_fetch_ts": marketwatch_rss_last_fetch or None},
            "thestreet": {"enabled": THESTREET_RSS_ENABLED, "last_fetch_ts": thestreet_rss_last_fetch or None},
            "verification_scores": _json_value(K_RSS_VERIFICATION_SCORES),
        },
        "calendar_relevance": {
            "llm_configured": calendar_llm_configured(),
            "pending": relevance_pending["total"],
            "live_pending": relevance_pending["live"],
            "backlog_pending": relevance_pending["backlog"],
            "failed": int(r.llen(CALENDAR_RELEVANCE_FAILED_KEY) or 0),
            "classified": int(r.scard(CALENDAR_RELEVANCE_CLASSIFIED_KEY) or 0),
            "heartbeat": _json_value(CALENDAR_RELEVANCE_HEARTBEAT_KEY),
        },
        "gdelt": {
            "enabled": GDELT_ENABLED,
            "query": GDELT_QUERY,
            "timespan": GDELT_TIMESPAN,
            "max_records": GDELT_MAX_RECORDS,
            "min_interval_seconds": GDELT_MIN_INTERVAL_SECONDS,
            "sort": GDELT_SORT,
            "export_enabled": GDELT_EXPORT_ENABLED,
            "export_min_interval_seconds": GDELT_EXPORT_MIN_INTERVAL_SECONDS,
            "export_catchup_max_batches": GDELT_EXPORT_CATCHUP_MAX_BATCHES,
            "export_last_check_ts": gdelt_export_last_check or None,
            "export_last_url": gdelt_export_last_url or None,
            "export_latest_url": gdelt_export_latest_url or None,
            "export_catchup_active": gdelt_export_backlog,
            "export_lag_batches": gdelt_export_lag_batches,
            "export_lag_minutes": gdelt_export_lag_batches * 15,
            "export_lag_alert_batches": GDELT_EXPORT_LAG_ALERT_BATCHES,
            "export_lag_alert": gdelt_export_lag_batches >= GDELT_EXPORT_LAG_ALERT_BATCHES,
            "event_collection": GDELT_EVENT_COLLECTION_NAME,
            "event_queue_pending": gdelt_event_pending,
            "event_queue_processing": gdelt_event_processing,
            "event_queue_dead_letter": gdelt_event_dead_letter,
            "last_fetch_ts": gdelt_last_fetch or None,
            "backfill_enabled": GDELT_BACKFILL_ENABLED,
            "backfill_days": GDELT_BACKFILL_DAYS,
            "backfill_chunk_hours": GDELT_BACKFILL_CHUNK_HOURS,
            "backfill_max_chunks": GDELT_BACKFILL_MAX_CHUNKS,
            "backfill_max_records": GDELT_BACKFILL_MAX_RECORDS,
            "backfill_min_interval_seconds": GDELT_BACKFILL_MIN_INTERVAL_SECONDS,
            "backfill_last_run_ts": gdelt_backfill_last_run or None,
            "backfill_cursor_ts": gdelt_backfill_cursor or None,
        },
        "backlog": {
            "recent_articles": recent_article_count,
            "recent_tickers": recent_ticker_count,
            "gdelt_event_queue_pending": gdelt_event_pending,
            "gdelt_event_queue_processing": gdelt_event_processing,
            "gdelt_event_queue_dead_letter": gdelt_event_dead_letter,
        },
    })


@router.get("/news/quality")
def news_quality(status: str = Query("open", pattern="^(open|reviewed|dismissed)$"), limit: int = Query(50, ge=1, le=100)):
    if not persistent_store.enabled():
        raise HTTPException(status_code=503, detail="News quality storage is unavailable")
    return {
        "report": persistent_store.latest_news_quality_report(),
        "counts": persistent_store.news_quality_issue_counts(),
        "status": status,
        "issues": persistent_store.list_news_quality_issues(status, limit),
    }


@router.post("/news/quality/issues/{issue_key}")
def review_news_quality_issue(
    issue_key: str,
    body: NewsQualityReview,
    _access_token: str = Depends(require_access_token),
):
    if not re.fullmatch(r"[0-9a-f]{64}", issue_key):
        raise HTTPException(status_code=400, detail="Invalid issue key")
    try:
        issue = persistent_store.review_news_quality_issue(issue_key, body.status, body.note)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not issue:
        raise HTTPException(status_code=404, detail="Quality issue not found")
    return issue

def _load_price_history(symbol: str, period: str = "3mo") -> pd.DataFrame:
    now_ts = pd.Timestamp.utcnow().timestamp()
    cache_key = (symbol, period)
    entry = _PRICE_CACHE.get(cache_key)
    if entry:
        ts, data = entry
        if (now_ts - ts) <= _PRICE_CACHE_TTL:
            return data.copy()

    # Try Redis price cache if configured
    if PRICE_SOURCE == "redis":
        try:
            key = PRICE_KEY_TEMPLATE.format(symbol=symbol)
            raw = r.get(key)
            if raw:
                rows = json.loads(raw)
                df = pd.DataFrame(rows)
                # Ensure required columns exist
                if set(["Open", "High", "Low", "Close", "Volume"]).issubset(df.columns):
                    df = df.reset_index(drop=True)
                    _PRICE_CACHE[cache_key] = (now_ts, df)
                    return df.copy()
        except Exception:
            pass

    # Fallback to yfinance
    try:
        df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
        if df is not None and not df.empty:
            df = df.rename(columns={"Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"})
            df = df.reset_index(drop=True)
            _PRICE_CACHE[cache_key] = (now_ts, df)
            return df.copy()
    except Exception:
        pass
    dummy_data = {
        "Open": [1,2,3,4,5,6],
        "High": [1,2,3,4,5,6],
        "Low": [1,2,3,4,5,6],
        "Close": [1,2,3,4,5,6],
        "Volume": [1,2,3,4,5,6],
    }
    dummy_df = pd.DataFrame(dummy_data)
    _PRICE_CACHE[cache_key] = (now_ts, dummy_df)
    return dummy_df.copy()

def _latest_price(df: pd.DataFrame) -> float:
    try:
        return float(df["Close"].iloc[-1])
    except Exception:
        return 0.0


def _simple_recommendation(df: pd.DataFrame, rag_score: float, rag_conf: float) -> dict:
    """
    Heuristic rec using momentum + RAG confidence.
    """
    try:
        closes = df["Close"].astype(float)
        if len(closes) < 10:
            return {"action": "hold", "reason": "insufficient history", "confidence": rag_conf}
        short_ma = closes.tail(5).mean()
        long_ma = closes.tail(20).mean() if len(closes) >= 20 else closes.mean()
        last = closes.iloc[-1]
        action = "hold"
        reason = "neutral"
        conf = min(1.0, 0.5 * rag_conf + 0.5 * (abs(short_ma - long_ma) / (long_ma + 1e-6)))
        if last > short_ma and short_ma > long_ma and rag_conf > 0.2:
            action = "buy"
            reason = "uptrend with positive RAG"
        elif last < long_ma and rag_conf < 0.3:
            action = "sell"
            reason = "downtrend with low RAG confidence"
        return {"action": action, "reason": reason, "confidence": conf}
    except Exception:
        return {"action": "hold", "reason": "error computing metrics", "confidence": rag_conf}


class _Memory:
    def __init__(self):
        self.actions = []
        self.states = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []

def _load_agent(obs_dim: int, action_dim: int):
    global _AGENT_CACHE
    if _AGENT_CACHE is not None:
        return _AGENT_CACHE
    weights_path = os.getenv("AGENT_WEIGHTS_PATH")
    if not weights_path or not os.path.exists(weights_path):
        return None
    try:
        import torch
        from drl_agent.agent import PPOAgent
    except Exception:
        return None
    lr = float(os.getenv("AGENT_LR", "0.0003"))
    betas = (0.9, 0.999)
    gamma = float(os.getenv("AGENT_GAMMA", "0.99"))
    K_epochs = int(os.getenv("AGENT_K_EPOCHS", "80"))
    eps_clip = float(os.getenv("AGENT_EPS_CLIP", "0.2"))
    agent = PPOAgent(obs_dim, action_dim, lr, betas, gamma, K_epochs, eps_clip)
    try:
        state_dict = torch.load(weights_path, map_location="cpu")
        agent.policy_old.load_state_dict(state_dict)
        agent.policy.load_state_dict(state_dict)
        _AGENT_CACHE = agent
        return agent
    except Exception:
        return None


def _get_cached_rec(symbol: str):
    entry = _RECOMMENDATION_CACHE.get(symbol)
    if not entry:
        return None
    ts, data = entry
    if (pd.Timestamp.utcnow().timestamp() - ts) > _RECOMMENDATION_TTL:
        return None
    return data


def _set_cached_rec(symbol: str, data: dict):
    _RECOMMENDATION_CACHE[symbol] = (pd.Timestamp.utcnow().timestamp(), data)

@router.get("/agent/recommendation")
def agent_recommendation(symbol: str = "AAPL"):
    """
    Lightweight endpoint to fetch an agent-informed score for a ticker using recent prices.
    Uses a trained policy if AGENT_WEIGHTS_PATH is provided; falls back to heuristic.
    """
    cached = _get_cached_rec(symbol)
    if cached:
        return JSONResponse(cached)

    df = _load_price_history(symbol, period="3mo")
    emb_dim = int(os.getenv("RAG_SIGNAL_EMBED_DIM", "16"))
    env = StockTradingEnv(df, symbol=symbol, embedding_dim=emb_dim, max_episode_steps=50)
    state = env.reset()
    # heuristic: hold to get current rag signal; reward reflects net-worth change (zero on hold)
    rag_score, rag_emb, rag_conf = env._get_rag_observation()  # uses RAG endpoint
    latest = _latest_price(df)
    rec = _simple_recommendation(df, rag_score, rag_conf)
    policy_used = "heuristic"
    reward = 0.0

    agent = _load_agent(env.observation_space.shape[0], env.action_space.n)
    if agent:
        mem = _Memory()
        action = agent.select_action(state, mem)
        _, reward, _, _ = env.step(action)
        rec = {"action": "buy" if action == 0 else "sell" if action == 1 else "hold", "reason": "policy_action", "confidence": rag_conf}
        policy_used = "ppo"

    resp = {
        "symbol": symbol,
        "rag_score": rag_score,
        "rag_embedding_dim": len(rag_emb),
        "rag_confidence": rag_conf,
        "latest_price": latest,
        "obs_len": len(state),
        "recommendation": rec,
        "reward": reward,
        "policy": policy_used,
        "note": "Returns PPO action if weights provided via AGENT_WEIGHTS_PATH; otherwise heuristic."
    }
    _set_cached_rec(symbol, resp)
    return JSONResponse(resp)
