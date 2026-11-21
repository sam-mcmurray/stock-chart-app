import logging
import os, time, json, re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

import redis

from news_sources.finnhub_source import load_finnhub_general_news
from news_sources.gdelt_source import GDELTRateLimitError, load_gdelt_articles
from news_sources.gdelt_export_source import (
    get_latest_gdelt_export_url,
    export_gap_batches,
    load_gdelt_export_mentions,
    load_gdelt_export_url,
    pending_export_urls,
)
from news_sources.newsapi_source import NewsAPIRateLimitError, load_newsapi_backfill_articles
from news_sources.rss_marketwatch import load_marketwatch_topstories
from news_sources.rss_yahoo import load_yahoo_rss
from news_sources.the_street import load_thestreet_rss
from services.article_metadata import enrich_articles
from services.article_dedup import article_dedup_id, deduplicate_articles, merge_articles
from services.article_graph import remove_superseded_article_nodes, upsert_article_graph
from services.news_rejection import exclude_known_rejections
from services import persistent_store
from services.calendar_event_extractor import news_article_calendar_event
from services.calendar_relevance_queue import enqueue as enqueue_calendar_relevance
from services.gdelt_event_queue import enqueue as enqueue_gdelt_events
from services.news_index_queue import enqueue as enqueue_news_index
from services.ticker_utils import extract_article_tickers, extract_tickers
from services.security_master import resolve_company_tickers

# If you have your own aggregator, import it:
# from news_aggregator import fetch_recent_news
# We'll provide a generic stub to integrate with your existing sources.

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)
logger = logging.getLogger(__name__)

NEWS_SCAN_INTERVAL = int(os.getenv("NEWS_SCAN_INTERVAL", "60"))
NEWS_LOOKBACK_DAYS = int(os.getenv("NEWS_LOOKBACK_DAYS", "14"))
GDELT_ENABLED = os.getenv("GDELT_ENABLED", "true").lower() == "true"
GDELT_QUERY = os.getenv("GDELT_QUERY", "business OR finance OR stocks OR market")
GDELT_TIMESPAN = os.getenv("GDELT_TIMESPAN", "1h")
GDELT_MAX_RECORDS = int(os.getenv("GDELT_MAX_RECORDS", "75"))
GDELT_MIN_INTERVAL_SECONDS = int(os.getenv("GDELT_MIN_INTERVAL_SECONDS", "900"))
GDELT_SORT = os.getenv("GDELT_SORT", "datedesc")
GDELT_EXPORT_ENABLED = os.getenv("GDELT_EXPORT_ENABLED", "true").lower() == "true"
GDELT_EXPORT_MIN_INTERVAL_SECONDS = int(os.getenv("GDELT_EXPORT_MIN_INTERVAL_SECONDS", "900"))
GDELT_EXPORT_CATCHUP_MAX_BATCHES = max(1, int(os.getenv("GDELT_EXPORT_CATCHUP_MAX_BATCHES", "16")))
GDELT_EXPORT_LAG_ALERT_BATCHES = max(1, int(os.getenv("GDELT_EXPORT_LAG_ALERT_BATCHES", "3")))
GDELT_ARTICLE_METADATA_ENABLED = os.getenv("GDELT_ARTICLE_METADATA_ENABLED", "true").lower() == "true"
GDELT_ARTICLE_METADATA_TIMEOUT_SECONDS = float(os.getenv("GDELT_ARTICLE_METADATA_TIMEOUT_SECONDS", "6"))
GDELT_ARTICLE_METADATA_MAX_ARTICLES = int(os.getenv("GDELT_ARTICLE_METADATA_MAX_ARTICLES", "40"))
GDELT_BACKFILL_ENABLED = os.getenv("GDELT_BACKFILL_ENABLED", "false").lower() == "true"
GDELT_BULK_IMPORT_ACTIVE = os.getenv("GDELT_BULK_IMPORT_ACTIVE", "false").lower() == "true"
GDELT_BACKFILL_DAYS = int(os.getenv("GDELT_BACKFILL_DAYS", "30"))
GDELT_BACKFILL_CHUNK_HOURS = int(os.getenv("GDELT_BACKFILL_CHUNK_HOURS", "24"))
GDELT_BACKFILL_MAX_CHUNKS = int(os.getenv("GDELT_BACKFILL_MAX_CHUNKS", "1"))
GDELT_BACKFILL_MAX_RECORDS = int(os.getenv("GDELT_BACKFILL_MAX_RECORDS", "75"))
GDELT_BACKFILL_MIN_INTERVAL_SECONDS = int(os.getenv("GDELT_BACKFILL_MIN_INTERVAL_SECONDS", str(24 * 60 * 60)))
GDELT_RATE_LIMIT_BACKOFF_SECONDS = int(os.getenv("GDELT_RATE_LIMIT_BACKOFF_SECONDS", str(6 * 60 * 60)))
NEWSAPI_ENABLED = os.getenv("NEWSAPI_ENABLED", "false").lower() == "true"
NEWSAPI_MIN_INTERVAL_SECONDS = int(os.getenv("NEWSAPI_MIN_INTERVAL_SECONDS", str(24 * 60 * 60)))
NEWSAPI_DAILY_REQUEST_LIMIT = int(os.getenv("NEWSAPI_DAILY_REQUEST_LIMIT", "90"))
NEWSAPI_BACKFILL_DAYS = int(os.getenv("NEWSAPI_BACKFILL_DAYS", "30"))
NEWSAPI_BACKFILL_QUERY = os.getenv("NEWSAPI_BACKFILL_QUERY", "business OR finance OR stocks OR market")
NEWSAPI_BACKFILL_PAGE_SIZE = int(os.getenv("NEWSAPI_BACKFILL_PAGE_SIZE", "100"))
NEWSAPI_BACKFILL_MAX_PAGES = int(os.getenv("NEWSAPI_BACKFILL_MAX_PAGES", "1"))
FINNHUB_NEWS_ENABLED = os.getenv("FINNHUB_NEWS_ENABLED", "true").lower() == "true"
FINNHUB_NEWS_MIN_INTERVAL_SECONDS = int(os.getenv("FINNHUB_NEWS_MIN_INTERVAL_SECONDS", "900"))
YAHOO_RSS_ENABLED = os.getenv("YAHOO_RSS_ENABLED", "true").lower() == "true"
MARKETWATCH_RSS_ENABLED = os.getenv("MARKETWATCH_RSS_ENABLED", "true").lower() == "true"
THESTREET_RSS_ENABLED = os.getenv("THESTREET_RSS_ENABLED", "true").lower() == "true"
NEWS_RSS_MIN_INTERVAL_SECONDS = int(os.getenv("NEWS_RSS_MIN_INTERVAL_SECONDS", "21600"))

# Redis keys
Z_ARTICLES = "news:recent:articles"     # ZSET score=published_ts, member=doc_id
H_ART_PREFIX = "news:article:"          # HASH per article id (url/title/tickers/...)
Z_TICKERS = "news:recent:tickers"       # ZSET score=last_seen_ts, member=TICKER
H_NEWS_HEARTBEAT = "news:heartbeat"
K_NEWSAPI_LAST_FETCH = "newsapi:last_fetch_ts"
K_NEWSAPI_RATE_LIMITED = "newsapi:rate_limited_ts"
K_FINNHUB_LAST_FETCH = "finnhub:news:last_fetch_ts"
K_YAHOO_RSS_LAST_FETCH = "news:rss:yahoo:last_fetch_ts"
K_MARKETWATCH_RSS_LAST_FETCH = "news:rss:marketwatch:last_fetch_ts"
K_THESTREET_RSS_LAST_FETCH = "news:rss:thestreet:last_fetch_ts"
K_RSS_VERIFICATION_SCORES = "news:rss:verification_scores"
K_GDELT_LAST_FETCH = "gdelt:last_fetch_ts"
K_GDELT_RATE_LIMITED = "gdelt:rate_limited_ts"
K_GDELT_EXPORT_LAST_CHECK = "gdelt:export:last_check_ts"
K_GDELT_EXPORT_LAST_URL = "gdelt:export:last_url"
K_GDELT_EXPORT_BACKLOG = "gdelt:export:backlog"
K_GDELT_EXPORT_LATEST_URL = "gdelt:export:latest_url"
K_GDELT_EXPORT_LAG_BATCHES = "gdelt:export:lag_batches"
K_GDELT_BACKFILL_LAST_RUN = "gdelt:backfill:last_run_ts"
K_GDELT_BACKFILL_CURSOR = "gdelt:backfill:cursor_ts"
K_TICKER_EXTRACTION_VERSION = "news:ticker_extraction_version"
TICKER_EXTRACTION_VERSION = "10"
K_NEWS_DEDUP_VERSION = "news:dedup_version"
K_NEWS_DEDUP_ORIGINAL_COUNT = "news:dedup:original_count"
K_NEWS_DEDUP_CONSOLIDATED_COUNT = "news:dedup:consolidated_count"
K_NEWS_VECTOR_DEDUP_VERSION = "news:vector_dedup_version"
K_NEWS_VECTOR_DEDUP_ORIGINAL_COUNT = "news:vector_dedup:original_count"
K_NEWS_VECTOR_DEDUP_CONSOLIDATED_COUNT = "news:vector_dedup:consolidated_count"
K_NEWS_GRAPH_DEDUP_VERSION = "news:graph_dedup_version"
K_NEWS_GRAPH_DEDUP_REMOVED_COUNT = "news:graph_dedup:removed_count"
H_NEWS_DEDUP_FINGERPRINTS = "news:dedup:fingerprints"
NEWS_DEDUP_VERSION = "1"
NEWS_VECTOR_DEDUP_VERSION = "1"
NEWS_GRAPH_DEDUP_VERSION = "1"
MAX_FUTURE_NEWS_SKEW_SECONDS = 15 * 60

# --- Helpers ---
def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())

def _now_ts() -> int:
    return _epoch(datetime.now(timezone.utc))

def _cutoff_ts() -> int:
    return _epoch(datetime.now(timezone.utc) - timedelta(days=NEWS_LOOKBACK_DAYS))

def simple_extract_tickers(text: str) -> List[str]:
    return extract_tickers(text)

def _doc_id_from_url(url: str) -> str:
    import hashlib
    return hashlib.sha1(url.encode("utf-8")).hexdigest()

def _newsapi_daily_key(now_ts: int | None = None) -> str:
    day = datetime.fromtimestamp(now_ts or _now_ts(), tz=timezone.utc).strftime("%Y%m%d")
    return f"newsapi:daily_count:{day}"

def _redis_int(key: str) -> int:
    try:
        return int(r.get(key) or 0)
    except Exception:
        return 0

def _newsapi_allowed() -> bool:
    if not NEWSAPI_ENABLED:
        return False
    now_ts = _now_ts()
    last_fetch = _redis_int(K_NEWSAPI_LAST_FETCH)
    if last_fetch and now_ts - last_fetch < NEWSAPI_MIN_INTERVAL_SECONDS:
        return False
    if _redis_int(_newsapi_daily_key(now_ts)) >= NEWSAPI_DAILY_REQUEST_LIMIT:
        return False
    return True

def _interval_allowed(key: str, min_interval_seconds: int, enabled: bool = True) -> bool:
    if not enabled:
        return False
    now_ts = _now_ts()
    last_fetch = _redis_int(key)
    return not last_fetch or now_ts - last_fetch >= min_interval_seconds

def _gdelt_allowed() -> bool:
    if not GDELT_ENABLED or GDELT_BULK_IMPORT_ACTIVE:
        return False
    limited_at = _redis_int(K_GDELT_RATE_LIMITED)
    return not limited_at or _now_ts() - limited_at >= GDELT_RATE_LIMIT_BACKOFF_SECONDS

def _mark_timestamp(key: str):
    try:
        r.set(key, _now_ts())
    except Exception:
        pass

def _newsapi_remaining_budget() -> int:
    if not NEWSAPI_ENABLED:
        return 0
    return max(0, NEWSAPI_DAILY_REQUEST_LIMIT - _redis_int(_newsapi_daily_key()))

def _mark_newsapi_request():
    now_ts = _now_ts()
    try:
        pipe = r.pipeline()
        pipe.set(K_NEWSAPI_LAST_FETCH, now_ts)
        daily_key = _newsapi_daily_key(now_ts)
        pipe.incr(daily_key)
        pipe.expire(daily_key, 48 * 60 * 60)
        pipe.execute()
    except Exception:
        pass

def _mark_newsapi_rate_limited():
    now_ts = _now_ts()
    try:
        r.set(K_NEWSAPI_RATE_LIMITED, now_ts, ex=24 * 60 * 60)
        r.set(K_NEWSAPI_LAST_FETCH, now_ts)
    except Exception:
        pass

def _mark_gdelt_rate_limited():
    now_ts = _now_ts()
    try:
        r.set(K_GDELT_RATE_LIMITED, now_ts, ex=GDELT_RATE_LIMIT_BACKOFF_SECONDS)
        r.set(K_GDELT_LAST_FETCH, now_ts)
        r.set(K_GDELT_BACKFILL_LAST_RUN, now_ts)
    except Exception:
        pass

def _article_exists(url: str) -> bool:
    if not url:
        return True
    try:
        canonical_id = article_dedup_id({"url": url})
        return bool(
            (canonical_id and r.exists(f"{H_ART_PREFIX}{canonical_id}"))
            or r.exists(f"{H_ART_PREFIX}{_doc_id_from_url(url)}")
        )
    except Exception:
        return False

def _article_has_other_provider(article: Dict[str, Any], provider: str) -> bool:
    url = article.get("url") or ""
    fingerprint = article.get("content_fingerprint") or ""
    candidate_ids = [article_dedup_id({"url": url}), _doc_id_from_url(url)]
    try:
        if fingerprint:
            candidate_ids.append(r.hget(H_NEWS_DEDUP_FINGERPRINTS, fingerprint))
        for aid in dict.fromkeys(candidate_ids):
            if not aid:
                continue
            raw = r.hget(f"{H_ART_PREFIX}{aid}", "json")
            if not raw:
                continue
            existing = json.loads(raw)
            providers = existing.get("providers") or [existing.get("provider") or "unknown"]
            return any(str(value).lower() != provider.lower() for value in providers)
    except (TypeError, ValueError, redis.RedisError):
        return False
    return False

def _normalize_article(article: Dict[str, Any], source_hint: str) -> Dict[str, Any]:
    title = article.get("headline") or article.get("title") or ""
    summary = article.get("summary") or article.get("description") or ""
    normalized = {
        "id": article.get("id"),
        "url": article.get("url"),
        "title": title,
        "summary": summary,
        "text": article.get("text") or summary or title,
        "published_ts": article.get("datetime") or article.get("published_ts"),
        "source": article.get("source") or source_hint,
        "provider": source_hint.lower(),
        "tickers": extract_article_tickers(title, article.get("tickers")),
        "metadata": dict(article.get("metadata") or {}),
    }
    for key in (
        "author", "event_id", "event_code", "gdelt_summary", "title_inferred",
        "metadata_enrichment_attempted", "location", "country_code", "actor1", "actor2",
    ):
        if article.get(key) not in (None, ""):
            normalized[key] = article[key]
    return deduplicate_articles([normalized])[0]

def _gdelt_datetime(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d%H%M%S")

def _fetch_gdelt_current() -> list[dict]:
    if not _gdelt_allowed() or not _interval_allowed(K_GDELT_LAST_FETCH, GDELT_MIN_INTERVAL_SECONDS, GDELT_ENABLED):
        return []
    _mark_timestamp(K_GDELT_LAST_FETCH)
    try:
        articles = load_gdelt_articles(
            query=GDELT_QUERY,
            timespan=GDELT_TIMESPAN,
            max_records=GDELT_MAX_RECORDS,
            sort=GDELT_SORT,
        )
    except GDELTRateLimitError as exc:
        logger.warning("GDELT rate limited; backing off for %ss: %s", GDELT_RATE_LIMIT_BACKOFF_SECONDS, exc)
        _mark_gdelt_rate_limited()
        return []
    fresh = [
        article
        for article in articles
        if not _article_exists(article.get("url") or "")
    ]
    if GDELT_ARTICLE_METADATA_ENABLED:
        limit = max(0, GDELT_ARTICLE_METADATA_MAX_ARTICLES)
        fresh = enrich_articles(
            fresh[:limit], timeout=GDELT_ARTICLE_METADATA_TIMEOUT_SECONDS
        ) + fresh[limit:]
    return [_normalize_article(article, "GDELT") for article in fresh]

def _fetch_gdelt_backfill() -> list[dict]:
    if not _gdelt_allowed() or not _interval_allowed(K_GDELT_BACKFILL_LAST_RUN, GDELT_BACKFILL_MIN_INTERVAL_SECONDS, GDELT_BACKFILL_ENABLED):
        return []
    now_ts = _now_ts()
    start_floor = now_ts - GDELT_BACKFILL_DAYS * 24 * 60 * 60
    cursor = _redis_int(K_GDELT_BACKFILL_CURSOR) or start_floor
    if cursor < start_floor or cursor >= now_ts:
        cursor = start_floor

    items: list[dict] = []
    chunk_seconds = max(1, GDELT_BACKFILL_CHUNK_HOURS) * 60 * 60
    for _ in range(max(1, GDELT_BACKFILL_MAX_CHUNKS)):
        chunk_start = cursor
        chunk_end = min(cursor + chunk_seconds, now_ts)
        if chunk_start >= chunk_end:
            break
        try:
            articles = load_gdelt_articles(
                query=GDELT_QUERY,
                start_datetime=_gdelt_datetime(chunk_start),
                end_datetime=_gdelt_datetime(chunk_end),
                max_records=GDELT_BACKFILL_MAX_RECORDS,
                sort="datedesc",
            )
        except GDELTRateLimitError as exc:
            logger.warning("GDELT backfill rate limited; backing off for %ss: %s", GDELT_RATE_LIMIT_BACKOFF_SECONDS, exc)
            _mark_gdelt_rate_limited()
            break
        items.extend(
            _normalize_article(article, "GDELT")
            for article in articles
            if not _article_exists(article.get("url") or "")
        )
        cursor = chunk_end

    try:
        r.set(K_GDELT_BACKFILL_CURSOR, cursor if cursor < now_ts else start_floor)
        r.set(K_GDELT_BACKFILL_LAST_RUN, now_ts)
    except Exception:
        pass
    return items


def _fetch_gdelt_export_cycle() -> tuple[list[dict], str, bool | None]:
    backlog_active = bool(_redis_int(K_GDELT_EXPORT_BACKLOG))
    if not backlog_active and not _interval_allowed(
        K_GDELT_EXPORT_LAST_CHECK,
        GDELT_EXPORT_MIN_INTERVAL_SECONDS,
        GDELT_EXPORT_ENABLED,
    ):
        return [], "", None
    if not GDELT_EXPORT_ENABLED:
        return [], "", None
    try:
        previous_url = r.get(K_GDELT_EXPORT_LAST_URL) or ""
    except Exception:
        previous_url = ""
    try:
        latest_url = get_latest_gdelt_export_url()
    except Exception as exc:
        logger.warning("GDELT latest export lookup failed: %s", exc)
        _mark_timestamp(K_GDELT_EXPORT_LAST_CHECK)
        return [], "", False

    try:
        pipe = r.pipeline()
        pipe.set(K_GDELT_EXPORT_LATEST_URL, latest_url)
        pipe.set(K_GDELT_EXPORT_LAG_BATCHES, export_gap_batches(previous_url, latest_url))
        pipe.execute()
    except Exception:
        logger.warning("GDELT export lag observation could not be stored")

    urls = pending_export_urls(previous_url, latest_url, GDELT_EXPORT_CATCHUP_MAX_BATCHES)
    articles = []
    completed_url = ""
    for export_url in urls:
        try:
            batch = load_gdelt_export_url(export_url)
        except Exception as exc:
            logger.warning("GDELT catch-up stopped at %s: %s", export_url, exc)
            break
        event_ids = {str(article.get("event_id")) for article in batch if article.get("event_id")}
        try:
            if event_ids:
                persistent_store.upsert_gdelt_mentions(
                    load_gdelt_export_mentions(export_url, event_ids, raise_on_error=True)
                )
        except Exception as exc:
            logger.warning("GDELT catch-up Mentions stopped at %s: %s", export_url, exc)
            break
        articles.extend(batch)
        completed_url = export_url

    caught_up = completed_url == latest_url or (not urls and previous_url == latest_url)
    if not urls and not previous_url and latest_url:
        caught_up = False
    if GDELT_ARTICLE_METADATA_ENABLED:
        limit = max(0, GDELT_ARTICLE_METADATA_MAX_ARTICLES)
        articles = enrich_articles(
            articles[:limit],
            timeout=GDELT_ARTICLE_METADATA_TIMEOUT_SECONDS,
        ) + articles[limit:]
    return [_normalize_article(article, "GDELT Export") for article in articles], completed_url, caught_up


def _commit_gdelt_export_cursor(completed_url: str, caught_up: bool | None) -> None:
    if caught_up is None:
        return
    try:
        pipe = r.pipeline()
        if completed_url:
            pipe.set(K_GDELT_EXPORT_LAST_URL, completed_url)
            latest_url = r.get(K_GDELT_EXPORT_LATEST_URL) or completed_url
            pipe.set(K_GDELT_EXPORT_LAG_BATCHES, export_gap_batches(completed_url, latest_url))
        if caught_up:
            pipe.delete(K_GDELT_EXPORT_BACKLOG)
            pipe.set(K_GDELT_EXPORT_LAG_BATCHES, 0)
            pipe.set(K_GDELT_EXPORT_LAST_CHECK, _now_ts())
        else:
            pipe.set(K_GDELT_EXPORT_BACKLOG, 1)
        pipe.execute()
    except Exception:
        logger.exception("GDELT export cursor could not be committed")


def _fetch_gdelt_export_current() -> list[dict]:
    articles, _completed_url, _caught_up = _fetch_gdelt_export_cycle()
    return articles

def _fetch_finnhub_current() -> list[dict]:
    if not _interval_allowed(K_FINNHUB_LAST_FETCH, FINNHUB_NEWS_MIN_INTERVAL_SECONDS, FINNHUB_NEWS_ENABLED):
        return []
    _mark_timestamp(K_FINNHUB_LAST_FETCH)
    return [
        _normalize_article(article, "Finnhub")
        for article in load_finnhub_general_news()
        if not _article_exists(article.get("url") or "")
    ]


def _fetch_rss_current(loader, key: str, enabled: bool, provider: str) -> list[dict]:
    if not _interval_allowed(key, NEWS_RSS_MIN_INTERVAL_SECONDS, enabled):
        return []
    _mark_timestamp(key)
    try:
        articles = loader()
    except Exception as exc:
        logger.warning("%s fetch failed: %s", provider, exc)
        return []
    normalized = []
    for article in articles:
        url = article.get("url") or ""
        if not url:
            continue
        item = _normalize_article(article, provider)
        # Known RSS URLs still need to reach the recent-cache merge so the
        # existing article records every provider that independently carried it.
        item["_already_known"] = _article_exists(url)
        item["_verification_match"] = _article_has_other_provider(item, provider)
        normalized.append(item)
    return normalized


def _record_rss_verification(items: List[Dict[str, Any]]) -> None:
    scored_providers = {"marketwatch rss", "thestreet rss"}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        provider = str(item.get("provider") or "").lower()
        if provider in scored_providers:
            grouped.setdefault(provider, []).append(item)
    if not grouped:
        return
    try:
        existing_raw = r.get(K_RSS_VERIFICATION_SCORES)
        scores = json.loads(existing_raw) if existing_raw else {}
    except (TypeError, ValueError, redis.RedisError):
        scores = {}
    checked_ts = _now_ts()
    for provider, provider_items in grouped.items():
        fetched = len(provider_items)
        matched = sum(bool(item.get("_verification_match")) for item in provider_items)
        scores[provider] = {
            "checked_ts": checked_ts,
            "fetched": fetched,
            "matched_by_other_provider": matched,
            "unmatched": fetched - matched,
            "coverage_score": round(matched / fetched, 4) if fetched else 0.0,
        }
    try:
        r.set(K_RSS_VERIFICATION_SCORES, json.dumps(scores))
    except redis.RedisError:
        logger.warning("RSS verification scores could not be stored")


# --- Integrate your aggregator here ---
def _fetch_news_cycle() -> tuple[List[Dict[str, Any]], str, bool | None]:
    """
    Fetches news from Finnhub and NewsAPI, aggregates them, and returns
    a list of dictionaries in the expected format.
    """
    all_news = []

    # GDELT is the primary near-realtime source. Other providers enrich and backfill it.
    all_news.extend(_fetch_gdelt_current())
    gdelt_export_items, completed_export_url, gdelt_export_caught_up = _fetch_gdelt_export_cycle()
    all_news.extend(gdelt_export_items)
    all_news.extend(_fetch_finnhub_current())
    all_news.extend(_fetch_rss_current(load_yahoo_rss, K_YAHOO_RSS_LAST_FETCH, YAHOO_RSS_ENABLED, "Yahoo RSS"))
    all_news.extend(_fetch_rss_current(load_marketwatch_topstories, K_MARKETWATCH_RSS_LAST_FETCH, MARKETWATCH_RSS_ENABLED, "MarketWatch RSS"))
    all_news.extend(_fetch_rss_current(load_thestreet_rss, K_THESTREET_RSS_LAST_FETCH, THESTREET_RSS_ENABLED, "TheStreet RSS"))
    all_news.extend(_fetch_gdelt_backfill())

    if _newsapi_allowed():
        try:
            now = datetime.now(timezone.utc)
            from_date = (now - timedelta(days=NEWSAPI_BACKFILL_DAYS)).date().isoformat()
            to_date = now.date().isoformat()
            max_pages = min(NEWSAPI_BACKFILL_MAX_PAGES, _newsapi_remaining_budget())
            for page in range(1, max_pages + 1):
                _mark_newsapi_request()
                newsapi_articles = load_newsapi_backfill_articles(
                    query=NEWSAPI_BACKFILL_QUERY,
                    from_date=from_date,
                    to_date=to_date,
                    page_size=NEWSAPI_BACKFILL_PAGE_SIZE,
                    page=page,
                )
                new_articles = [
                    _normalize_article(article, "NewsAPI")
                    for article in newsapi_articles
                    if not _article_exists(article.get("url") or "")
                ]
                all_news.extend(new_articles)
                if len(newsapi_articles) < NEWSAPI_BACKFILL_PAGE_SIZE:
                    break
        except NewsAPIRateLimitError as exc:
            logger.warning("NewsAPI rate limited; backing off for 24h: %s", exc)
            _mark_newsapi_rate_limited()
    return all_news, completed_export_url, gdelt_export_caught_up


def _fetch_news() -> List[Dict[str, Any]]:
    return _fetch_news_cycle()[0]

class NewsWorker:
    def __init__(self, indexer=None, gdelt_indexer=None):
        self.indexer = indexer
        self.gdelt_indexer = gdelt_indexer
        self._stop = False

    def stop(self): self._stop = True

    def _index_items(self, items: List[Dict[str, Any]]):
        gdelt_events = []
        supplemental = []
        for item in items:
            if item.get("_already_known"):
                continue
            if item.get("provider") == "gdelt export" and item.get("id") and item.get("event_id"):
                gdelt_events.append(item)
            else:
                supplemental.append(item)
        if supplemental:
            consolidated = deduplicate_articles(supplemental)
            for item in consolidated:
                item["id"] = item.get("dedup_id")
                item.setdefault("metadata", {})["providers"] = ",".join(item.get("providers") or [item.get("provider") or "unknown"])
                item["metadata"]["canonical_url"] = item.get("canonical_url") or item.get("url") or ""
            if self.indexer is not None:
                self.indexer.upsert_batch(consolidated)
            else:
                enqueue_news_index(consolidated, client=r)
        if gdelt_events:
            if self.gdelt_indexer is not None:
                self.gdelt_indexer.upsert_batch(gdelt_events)
            else:
                enqueue_gdelt_events(gdelt_events, client=r)

    def _heartbeat(self, status: str, **extra):
        payload = {"status": status, "ts": _now_ts(), **extra}
        try:
            r.set(H_NEWS_HEARTBEAT, json.dumps(payload))
        except Exception:
            pass

    def _prune_old(self):
        cutoff = _cutoff_ts()
        # Remove old articles
        old = r.zrangebyscore(Z_ARTICLES, 0, cutoff)
        if old:
            # delete hashes
            pipe = r.pipeline()
            for aid in old:
                pipe.delete(f"{H_ART_PREFIX}{aid}")
            pipe.execute()
            # remove from zset
            r.zremrangebyscore(Z_ARTICLES, 0, cutoff)
        # prune tickers window
        r.zremrangebyscore(Z_TICKERS, 0, cutoff)

    def _add_recent(self, items: List[Dict[str, Any]]):
        pipe = r.pipeline()
        now_ts = _now_ts()
        for it in deduplicate_articles(items):
            url = it.get("url") or ""
            if not url: continue
            aid = it.get("dedup_id") or article_dedup_id(it)
            fingerprint = it.get("content_fingerprint") or ""
            if fingerprint:
                aid = r.hget(H_NEWS_DEDUP_FINGERPRINTS, fingerprint) or aid
            existing_raw = r.hget(f"{H_ART_PREFIX}{aid}", "json")
            if existing_raw:
                try:
                    it = merge_articles(json.loads(existing_raw), it)
                except (TypeError, ValueError):
                    pass
            ts = int(it.get("published_ts") or now_ts)
            if ts > now_ts + MAX_FUTURE_NEWS_SKEW_SECONDS:
                ts = now_ts
            meta = {
                "id": it.get("id"),
                "url": url,
                "title": it.get("title") or "",
                "summary": it.get("summary") or "",
                "source": it.get("source") or "unknown",
                "provider": it.get("provider") or "unknown",
                "providers": it.get("providers") or [it.get("provider") or "unknown"],
                "canonical_url": it.get("canonical_url") or url,
                "source_urls": it.get("source_urls") or [url],
                "event_ids": it.get("event_ids") or ([str(it["event_id"])] if it.get("event_id") else []),
                "dedup_id": aid,
                "content_fingerprint": fingerprint,
                "published_ts": ts,
                "tickers": it.get("tickers") or [],
            }
            if it.get("metadata"):
                meta["metadata"] = it["metadata"]
            for key in (
                "author", "event_id", "event_code", "gdelt_summary", "title_inferred",
                "metadata_enrichment_attempted", "location", "country_code", "actor1", "actor2",
            ):
                if it.get(key) not in (None, ""):
                    meta[key] = it[key]
            pipe.hset(f"{H_ART_PREFIX}{aid}", mapping={"json": json.dumps(meta)})
            pipe.zadd(Z_ARTICLES, {aid: ts})
            if fingerprint:
                pipe.hset(H_NEWS_DEDUP_FINGERPRINTS, fingerprint, aid)
            # tickers last-seen
            for t in meta["tickers"]:
                pipe.zadd(Z_TICKERS, {t: ts})
        pipe.execute()

    def _migrate_recent_dedup(self):
        if r.get(K_NEWS_DEDUP_VERSION) == NEWS_DEDUP_VERSION:
            return
        article_rows = r.zrangebyscore(Z_ARTICLES, _cutoff_ts(), "+inf", withscores=True)
        articles = []
        old_ids = []
        for aid, _score in article_rows:
            raw = r.hget(f"{H_ART_PREFIX}{aid}", "json")
            if not raw:
                continue
            try:
                articles.append(json.loads(raw))
                old_ids.append(aid)
            except (TypeError, ValueError):
                continue
        consolidated = deduplicate_articles(articles)
        pipe = r.pipeline()
        pipe.delete(Z_ARTICLES)
        pipe.delete(H_NEWS_DEDUP_FINGERPRINTS)
        for aid in old_ids:
            pipe.delete(f"{H_ART_PREFIX}{aid}")
        pipe.execute()
        self._add_recent(consolidated)
        r.set(K_NEWS_DEDUP_ORIGINAL_COUNT, len(articles))
        r.set(K_NEWS_DEDUP_CONSOLIDATED_COUNT, len(consolidated))
        r.set(K_NEWS_DEDUP_VERSION, NEWS_DEDUP_VERSION)
        upsert_article_graph(consolidated)
        logger.info("Consolidated recent news from %s to %s articles", len(articles), len(consolidated))

    def _migrate_vector_dedup(self):
        if r.get(K_NEWS_VECTOR_DEDUP_VERSION) == NEWS_VECTOR_DEDUP_VERSION:
            return
        if self.indexer is None:
            return
        collection = self.indexer.col
        total = collection.count()
        items = []
        old_ids = []
        page_size = 500
        for offset in range(0, total, page_size):
            payload = collection.get(
                limit=page_size,
                offset=offset,
                include=["documents", "metadatas"],
            )
            ids = payload.get("ids") or []
            documents = payload.get("documents") or []
            metadatas = payload.get("metadatas") or []
            for index, old_id in enumerate(ids):
                metadata = dict(metadatas[index] or {}) if index < len(metadatas) else {}
                url = metadata.get("url") or ""
                if not url:
                    continue
                items.append({
                    "url": url,
                    "title": metadata.get("title") or "",
                    "text": documents[index] if index < len(documents) else metadata.get("title") or "",
                    "summary": documents[index] if index < len(documents) else "",
                    "published_ts": metadata.get("published_ts") or 0,
                    "source": metadata.get("source") or "unknown",
                    "provider": metadata.get("provider") or "unknown",
                    "tickers": metadata.get("tickers") or [],
                })
                old_ids.append(old_id)

        consolidated = deduplicate_articles(items)
        for item in consolidated:
            item["id"] = item.get("dedup_id")
            item.setdefault("metadata", {})["providers"] = ",".join(item.get("providers") or [item.get("provider") or "unknown"])
            item["metadata"]["canonical_url"] = item.get("canonical_url") or item.get("url") or ""
        self.indexer.upsert_batch(consolidated)
        retained_ids = {item.get("id") for item in consolidated}
        superseded = [old_id for old_id in old_ids if old_id not in retained_ids]
        for start in range(0, len(superseded), page_size):
            collection.delete(ids=superseded[start:start + page_size])
        r.set(K_NEWS_VECTOR_DEDUP_ORIGINAL_COUNT, len(items))
        r.set(K_NEWS_VECTOR_DEDUP_CONSOLIDATED_COUNT, len(consolidated))
        r.set(K_NEWS_VECTOR_DEDUP_VERSION, NEWS_VECTOR_DEDUP_VERSION)
        logger.info("Consolidated supplemental news vectors from %s to %s", len(items), len(consolidated))

    def _migrate_graph_dedup(self):
        if r.get(K_NEWS_GRAPH_DEDUP_VERSION) == NEWS_GRAPH_DEDUP_VERSION:
            return
        article_rows = r.zrangebyscore(Z_ARTICLES, _cutoff_ts(), "+inf")
        articles = []
        for aid in article_rows:
            raw = r.hget(f"{H_ART_PREFIX}{aid}", "json")
            if not raw:
                continue
            try:
                articles.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        canonical_urls = {
            item.get("canonical_url") or item.get("url")
            for item in articles
            if item.get("canonical_url") or item.get("url")
        }
        superseded_urls = sorted({
            url
            for item in articles
            for url in (item.get("source_urls") or [])
            if url and url not in canonical_urls
        })
        upsert_article_graph(articles)
        removed = remove_superseded_article_nodes(superseded_urls)
        if removed < 0:
            return
        r.set(K_NEWS_GRAPH_DEDUP_REMOVED_COUNT, removed)
        r.set(K_NEWS_GRAPH_DEDUP_VERSION, NEWS_GRAPH_DEDUP_VERSION)
        logger.info("Removed %s superseded Article nodes from the graph", removed)

    def _migrate_recent_tickers(self):
        if r.get(K_TICKER_EXTRACTION_VERSION) == TICKER_EXTRACTION_VERSION:
            return

        rebuilt: Dict[str, int] = {}
        now_ts = _now_ts()
        article_rows = r.zrangebyscore(Z_ARTICLES, _cutoff_ts(), "+inf", withscores=True)
        pipe = r.pipeline()
        pipe.delete(Z_TICKERS)
        for aid, score in article_rows:
            raw = r.hget(f"{H_ART_PREFIX}{aid}", "json")
            if not raw:
                continue
            try:
                meta = json.loads(raw)
            except (TypeError, ValueError):
                continue
            score = int(score)
            if score > now_ts + MAX_FUTURE_NEWS_SKEW_SECONDS:
                score = now_ts
                meta["published_ts"] = score
                pipe.zadd(Z_ARTICLES, {aid: score})
            tickers = sorted(set(
                extract_article_tickers(meta.get("title") or "")
                + resolve_company_tickers(f"{meta.get('title') or ''} {meta.get('summary') or ''}")
            ))
            meta["tickers"] = tickers
            pipe.hset(f"{H_ART_PREFIX}{aid}", mapping={"json": json.dumps(meta)})
            for ticker in tickers:
                rebuilt[ticker] = max(rebuilt.get(ticker, 0), score)
        if rebuilt:
            pipe.zadd(Z_TICKERS, rebuilt)
        pipe.set(K_TICKER_EXTRACTION_VERSION, TICKER_EXTRACTION_VERSION)
        pipe.execute()
        upsert_article_graph([
            json.loads(raw)
            for aid, _score in article_rows
            if (raw := r.hget(f"{H_ART_PREFIX}{aid}", "json"))
        ])
        logger.info("Rebuilt recent ticker candidates with extractor v%s: %s symbols", TICKER_EXTRACTION_VERSION, len(rebuilt))

    def run_forever(self):
        while not self._stop:
            try:
                started = _now_ts()
                self._prune_old()
                self._migrate_recent_dedup()
                self._migrate_vector_dedup()
                self._migrate_graph_dedup()
                self._migrate_recent_tickers()
                # Fetch news
                items, completed_export_url, gdelt_export_caught_up = _fetch_news_cycle()
                items = exclude_known_rejections(items)
                # fallback ticker extraction if not present
                for it in items:
                    if not it.get("tickers"):
                        it["tickers"] = simple_extract_tickers(it.get("title") or "")
                _record_rss_verification(items)
                # Index to vector store
                self._index_items(items)
                # Save to redis (recent window)
                self._add_recent(items)
                if persistent_store.enabled():
                    calendar_events = [
                        news_article_calendar_event(item)
                        for item in deduplicate_articles(items)
                        if item.get("url")
                    ]
                    persistent_store.upsert_calendar_events(calendar_events)
                    enqueue_calendar_relevance(calendar_events, client=r)
                graph_items = [item for item in items if not item.get("_already_known")]
                upsert_article_graph(deduplicate_articles(graph_items))
                _commit_gdelt_export_cursor(completed_export_url, gdelt_export_caught_up)
                provider_counts = {}
                provider_new_counts = {}
                for item in items:
                    provider = item.get("provider") or "unknown"
                    provider_counts[provider] = provider_counts.get(provider, 0) + 1
                    if not item.get("_already_known"):
                        provider_new_counts[provider] = provider_new_counts.get(provider, 0) + 1
                self._heartbeat(
                    "ok",
                    started_ts=started,
                    item_count=len(items),
                    provider_counts=provider_counts,
                    provider_new_counts=provider_new_counts,
                )
            except Exception as e:
                logger.exception("News worker cycle failed")
                self._heartbeat("error", error=str(e))
            finally:
                time.sleep(NEWS_SCAN_INTERVAL)

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting news worker")
    worker = NewsWorker()
    worker.run_forever()
