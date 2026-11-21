"""
news_historical.py
===================

This module provides a utility function for collecting historical news
articles about publicly traded companies using several free news APIs.
It can query NewsAPI, MarketAux and Alpha Vantage from a company's
IPO date up to the present day.  The goal is to build a chronological
archive of articles suitable for analysis or ingestion into a vector
database.

Notes and assumptions:

* Each API has its own rate limits on free plans.  The function
  respects these limits by limiting pagination and the number of
  articles fetched per API.  You may need to adjust these defaults
  depending on your usage and the limits of your account.
* You must supply your own API keys for the three services.  They
  should be passed into the function via keyword arguments.  If you
  don't want to use a particular API, pass ``None`` as its key.
* The function expects the IPO date to be provided as a string in
  ``YYYY-MM-DD`` format.  If you don't know the IPO date, you can
  look it up using Alpha Vantage's ``OVERVIEW`` endpoint or another
  data source, then pass it to this function.

Example usage:

    from news_historical import collect_historical_news

    articles = collect_historical_news(
        symbol="AAPL",
        company_name="Apple Inc.",
        ipo_date="1980-12-12",
        newsapi_key="<YOUR_NEWSAPI_KEY>",
        marketaux_token="<YOUR_MARKETAUX_TOKEN>",
        alpha_vantage_key="<YOUR_ALPHA_KEY>"
    )
    print(len(articles), articles[0])

"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

def _daterange_chunks(start_date: datetime.date, end_date: datetime.date, days: int) -> List[tuple[str, str]]:
    """Yield date range tuples (from_date, to_date) spanning the
    interval [start_date, end_date] in increments of ``days``.

    NewsAPI limits searches to a two‑week window on the free tier.  This
    helper splits long periods into smaller ranges.
    """
    ranges = []
    current_start = start_date
    while current_start <= end_date:
        current_end = min(current_start + datetime.timedelta(days=days - 1), end_date)
        ranges.append((current_start.isoformat(), current_end.isoformat()))
        current_start = current_end + datetime.timedelta(days=1)
    return ranges

def fetch_newsapi_range(symbol: str, company_name: str, from_date: str, to_date: str,
                        api_key: str, max_pages: int = 1) -> List[Dict]:
    """Fetch articles from NewsAPI between two dates.

    Returns a list of article dictionaries.  Limits to ``max_pages`` to
    avoid exceeding free tier quotas (each page returns up to 100
    results).
    """
    if not api_key:
        return []
    base_url = "https://newsapi.org/v2/everything"
    articles: List[Dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "q": f"(\"{company_name}\" OR {symbol})",
            "from": from_date,
            "to": to_date,
            "language": "en",
            "pageSize": 100,
            "page": page,
            "apiKey": api_key,
            "sortBy": "publishedAt",
        }
        try:
            resp = requests.get(base_url, params=params, timeout=30)
            data = resp.json()
        except Exception as exc:
            logger.warning("NewsAPI request failed: %s", exc)
            break
        if data.get("status") != "ok":
            logger.warning("NewsAPI returned error: %s", data.get("message"))
            break
        batch = data.get("articles", [])
        articles.extend(batch)
        # stop if fewer results than expected (no more pages)
        if len(batch) < params["pageSize"]:
            break
    return articles

def fetch_marketaux_range(symbol: str, from_date: str, to_date: str,
                          api_token: str, max_pages: int = 1) -> List[Dict]:
    """Fetch articles from MarketAux within a date range.

    The MarketAux API supports filtering by publication dates using
    ``published_after`` and ``published_before``.  Results include
    sentiment and entity data.  Limits to ``max_pages`` for rate control.
    """
    if not api_token:
        return []
    base_url = "https://api.marketaux.com/v1/news/all"
    articles: List[Dict] = []
    # convert ISO date to ISO 8601 with T in between for more precise filtering
    published_after = from_date + "T00:00:00"
    published_before = to_date + "T23:59:59"
    for page in range(1, max_pages + 1):
        params = {
            "api_token": api_token,
            "symbols": symbol,
            "language": "en",
            "published_after": published_after,
            "published_before": published_before,
            "filter_entities": True,
            "page": page,
        }
        try:
            resp = requests.get(base_url, params=params, timeout=30)
            data = resp.json()
        except Exception as exc:
            logger.warning("MarketAux request failed: %s", exc)
            break
        batch = data.get("data", [])
        articles.extend(batch)
        meta = data.get("meta", {})
        returned = meta.get("returned", 0)
        limit = meta.get("limit", 0)
        # if returned less than limit, no more pages
        if returned < limit:
            break
    return articles

def fetch_alpha_news(symbol: str, from_date: str, to_date: str,
                     api_key: str) -> List[Dict]:
    """Fetch news from Alpha Vantage's NEWS_SENTIMENT endpoint.

    Alpha Vantage does not provide a direct ``to_date`` parameter.  We use
    ``time_from`` to specify the earliest date; the API returns up to
    1000 articles after that time.  We filter locally for articles
    published before ``to_date``.
    """
    if not api_key:
        return []
    base_url = "https://www.alphavantage.co/query"
    time_from = from_date.replace("-", "") + "T0000"
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": symbol,
        "time_from": time_from,
        "sort": "LATEST",
        "limit": 1000,
        "apikey": api_key,
    }
    try:
        resp = requests.get(base_url, params=params, timeout=30)
        data = resp.json()
    except Exception as exc:
        logger.warning("Alpha Vantage request failed: %s", exc)
        return []
    articles = data.get("feed", [])
    # filter by to_date
    end_dt = datetime.datetime.strptime(to_date, "%Y-%m-%d").date()
    filtered = []
    for item in articles:
        pub_ts = item.get("time_published")  # format YYYYMMDDTHHMMSSZ
        try:
            pub_date = datetime.datetime.strptime(pub_ts[:8], "%Y%m%d").date()
        except Exception:
            continue
        if pub_date <= end_dt:
            filtered.append(item)
    return filtered

def collect_historical_news(symbol: str, company_name: str, ipo_date: str,
                            newsapi_key: Optional[str] = None,
                            marketaux_token: Optional[str] = None,
                            alpha_vantage_key: Optional[str] = None,
                            max_days_per_chunk: int = 14,
                            max_newsapi_pages: int = 1,
                            max_marketaux_pages: int = 1
                           ) -> List[Dict]:
    """Collect historical news articles about a company.

    This function queries multiple free news APIs from the company's IPO
    date up to today.  Results are merged into a single list with a
    ``source`` field indicating which API produced the article.

    Parameters
    ----------
    symbol : str
        The stock ticker symbol (e.g., "AAPL").
    company_name : str
        The full company name used for NewsAPI search (e.g., "Apple Inc.").
    ipo_date : str
        The company's IPO date in "YYYY-MM-DD" format.
    newsapi_key : str, optional
        Your NewsAPI API key.  If None, NewsAPI will not be used.
    marketaux_token : str, optional
        Your MarketAux API token.  If None, MarketAux will not be used.
    alpha_vantage_key : str, optional
        Your Alpha Vantage API key.  If None, Alpha Vantage will not be used.
    max_days_per_chunk : int, default 14
        Maximum number of days in each NewsAPI search window.  NewsAPI
        free plans are limited to 100 results per request and a two‑week
        search range.
    max_newsapi_pages : int, default 1
        Maximum number of NewsAPI pages to fetch per window.  Each page
        returns up to 100 articles.
    max_marketaux_pages : int, default 1
        Maximum number of MarketAux pages to fetch per date window.

    Returns
    -------
    List[Dict]
        A list of article objects from all available sources.  Each
        article contains at least ``source_name``, ``title``, ``url``
        and ``published_at`` (timestamps vary per API).

    Notes
    -----
    Because the APIs return different fields, this function does not
    enforce a uniform schema.  It simply attaches a ``_provider`` key
    to each article indicating which API returned it.  Downstream
    processing should handle the differences.
    """
    start_date = datetime.datetime.strptime(ipo_date, "%Y-%m-%d").date()
    end_date = datetime.date.today()
    all_articles: List[Dict] = []
    # Query NewsAPI in chunks
    if newsapi_key:
        for from_date, to_date in _daterange_chunks(start_date, end_date, max_days_per_chunk):
            logger.info("Fetching NewsAPI articles %s to %s", from_date, to_date)
            news = fetch_newsapi_range(symbol, company_name, from_date, to_date,
                                       newsapi_key, max_pages=max_newsapi_pages)
            for article in news:
                article["_provider"] = "NewsAPI"
            all_articles.extend(news)
    # Query MarketAux
    if marketaux_token:
        logger.info("Fetching MarketAux articles")
        market_news = fetch_marketaux_range(symbol, ipo_date, end_date.isoformat(),
                                           marketaux_token, max_pages=max_marketaux_pages)
        for article in market_news:
            article["_provider"] = "MarketAux"
        all_articles.extend(market_news)
    # Query Alpha Vantage
    if alpha_vantage_key:
        logger.info("Fetching Alpha Vantage articles")
        alpha_news = fetch_alpha_news(symbol, ipo_date, end_date.isoformat(), alpha_vantage_key)
        for article in alpha_news:
            article["_provider"] = "AlphaVantage"
        all_articles.extend(alpha_news)
    return all_articles

if __name__ == "__main__":
    import argparse
    import os
    import json

    parser = argparse.ArgumentParser(description="Collect historical news for a stock.")
    parser.add_argument("symbol", help="Ticker symbol, e.g., AAPL")
    parser.add_argument("company_name", help="Full company name for NewsAPI searches")
    parser.add_argument("ipo_date", help="IPO date in YYYY-MM-DD format")
    parser.add_argument("--newsapi_key", default=os.environ.get("NEWSAPI_KEY"), help="NewsAPI key")
    parser.add_argument("--marketaux_token", default=os.environ.get("MARKETAUX_TOKEN"), help="MarketAux token")
    parser.add_argument("--alpha_key", default=os.environ.get("ALPHA_VANTAGE_KEY"), help="Alpha Vantage key")
    parser.add_argument("--output", default="articles.json", help="Output JSON file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    articles = collect_historical_news(
        symbol=args.symbol,
        company_name=args.company_name,
        ipo_date=args.ipo_date,
        newsapi_key=args.newsapi_key,
        marketaux_token=args.marketaux_token,
        alpha_vantage_key=args.alpha_key,
    )
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    logger.info("Saved %d articles to %s", len(articles), args.output)