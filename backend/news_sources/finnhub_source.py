# core/news_sources/finnhub_source.py
import os
import requests
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def load_finnhub_news(symbol: str):
    now = datetime.now()
    from_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")
    
    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": symbol,
        "from": from_date,
        "to": to_date,
        "token": os.getenv("FINNHUB_API_KEY")
    }

    try:
        res = requests.get(url, params=params, timeout=20)
        res.raise_for_status()
        raw = res.json()
        return [{
            "source": a["source"],
            "headline": a["headline"],
            "summary": a["summary"],
            "url": a["url"],
            "datetime": a["datetime"],
            "id": a["id"],
            "related": a["related"],
            "image": a.get("image", "")
        } for a in raw]
    except Exception as e:
        logger.warning("Finnhub company news failed: %s", e)
        return []

def load_finnhub_general_news():
    if not os.getenv("FINNHUB_API_KEY"):
        return []
    url = "https://finnhub.io/api/v1/news"  # Market News endpoint
    params = {
        "category": "general",
        "token": os.getenv("FINNHUB_API_KEY")
    }

    try:
        res = requests.get(url, params=params, timeout=20)
        res.raise_for_status()
        raw = res.json()
        return [{
            "source": a["source"],
            "headline": a["headline"],
            "summary": a["summary"],
            "url": a["url"],
            "datetime": a["datetime"], # Unix timestamp
            "id": a["id"],
            "related": a["related"],
            "image": a.get("image", "")
        } for a in raw]
    except Exception as e:
        logger.warning("Finnhub general news failed: %s", e)
        return []
