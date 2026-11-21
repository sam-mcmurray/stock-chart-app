# core/news_sources/rss_yahoo.py
import feedparser
import calendar
import hashlib
import logging

logger = logging.getLogger(__name__)

def load_yahoo_rss():
    rss_url = "https://finance.yahoo.com/news/rss"
    feed = feedparser.parse(rss_url)
    if getattr(feed, "bozo", False) and not feed.entries:
        logger.warning("Yahoo Finance RSS failed: %s", getattr(feed, "bozo_exception", "invalid feed"))
        return []
    return [{
        "source": "Yahoo Finance",
        "headline": e.get("title", ""),
        "summary": e.get("summary", ""),
        "url": e.get("link", ""),
        "datetime": int(calendar.timegm(e.get("published_parsed"))) if e.get("published_parsed") else 0,
        "id": hashlib.sha1(e.get("link", "").encode("utf-8")).hexdigest(),
        "related": [],
        "image": ""
    } for e in feed.entries if e.get("title") and e.get("link")]
