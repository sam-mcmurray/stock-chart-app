# core/news_sources/rss_marketwatch.py
import feedparser
import calendar
import hashlib
import logging

logger = logging.getLogger(__name__)


def load_marketwatch_topstories():
    feed = feedparser.parse("https://feeds.marketwatch.com/marketwatch/topstories/")
    if getattr(feed, "bozo", False) and not feed.entries:
        logger.warning("MarketWatch RSS failed: %s", getattr(feed, "bozo_exception", "invalid feed"))
        return []
    return [{
        "source": "MarketWatch",
        "headline": entry.get("title", ""),
        "summary": entry.get("summary", ""),
        "url": entry.get("link", ""),
        "datetime": int(calendar.timegm(entry.get("published_parsed"))) if entry.get("published_parsed") else 0,
        "id": hashlib.sha1(entry.get("link", "").encode("utf-8")).hexdigest(),
        "related": [],
        "image": "",
    } for entry in feed.entries if entry.get("title") and entry.get("link")]

def load_marketwatch_rss(symbol: str):
    symbol = symbol.upper()
    return [
        article for article in load_marketwatch_topstories()
        if symbol in article["headline"].upper() or symbol in article["summary"].upper()
    ]
