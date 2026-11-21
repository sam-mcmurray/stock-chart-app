import os
import logging
import requests
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

class NewsAPIRateLimitError(RuntimeError):
    pass

def _api_key() -> str:
    return os.getenv("NEWSAPI_KEY", "")

def load_newsapi_articles(symbol: str):
    if not _api_key():
        return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": symbol,
        "apiKey": _api_key(),
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 20
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 429:
            raise NewsAPIRateLimitError("NewsAPI rate limit exceeded")
        r.raise_for_status()
        data = r.json()
        return [{
            "source": a["source"]["name"],
            "headline": a["title"],
            "summary": a["description"],
            "url": a["url"],
            "datetime": int(datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")).timestamp()),
            "id": hash(a["url"]),
            "related": [],
            "image": a.get("urlToImage", "")
        } for a in data.get("articles", []) if a.get("title")]
    except Exception as e:
        logger.warning("NewsAPI symbol news fetch failed for %s: %s", symbol, e)
        return []

def _article_payload(article: dict) -> dict:
    return {
        "source": (article.get("source") or {}).get("name"),
        "headline": article.get("title"),
        "summary": article.get("description"),
        "url": article.get("url"),
        "datetime": int(datetime.fromisoformat(article.get("publishedAt", "").replace("Z", "+00:00")).timestamp()),
        "id": hash(article.get("url")),
        "related": [],
        "image": article.get("urlToImage", "")
    }

def load_newsapi_general_articles():
    if not _api_key():
        return []
    url = "https://newsapi.org/v2/top-headlines"
    params = {
        "country": "us",
        "category": "business", # or general, technology, finance etc.
        "apiKey": _api_key(),
        "language": "en",
        "pageSize": 50 # Fetch a reasonable number of articles
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 429:
            raise NewsAPIRateLimitError("NewsAPI rate limit exceeded")
        r.raise_for_status()
        data = r.json()
        return [_article_payload(a) for a in data.get("articles", []) if a.get("title") and a.get("url")]
    except NewsAPIRateLimitError:
        raise
    except Exception as e:
        logger.warning("NewsAPI general news fetch failed: %s", e)
        return []

def load_newsapi_backfill_articles(
    query: str = 'business OR finance OR stocks OR market',
    from_date: str | None = None,
    to_date: str | None = None,
    page_size: int = 100,
    page: int = 1,
):
    if not _api_key():
        return []
    now = datetime.now(timezone.utc)
    if not to_date:
        to_date = now.date().isoformat()
    if not from_date:
        from_date = (now - timedelta(days=30)).date().isoformat()

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "from": from_date,
        "to": to_date,
        "apiKey": _api_key(),
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "page": page,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 429:
            raise NewsAPIRateLimitError("NewsAPI rate limit exceeded")
        r.raise_for_status()
        data = r.json()
        return [_article_payload(a) for a in data.get("articles", []) if a.get("title") and a.get("url")]
    except NewsAPIRateLimitError:
        raise
    except Exception as e:
        logger.warning("NewsAPI backfill fetch failed: %s", e)
        return []
