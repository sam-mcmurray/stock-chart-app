from types import SimpleNamespace

from news_sources import rss_marketwatch, rss_yahoo, the_street


PUBLISHED = (2026, 7, 19, 12, 30, 0, 0, 0, 0)


def _feed(entries):
    return SimpleNamespace(entries=entries, bozo=False)


def test_yahoo_rss_normalizes_current_entries(monkeypatch):
    monkeypatch.setattr(rss_yahoo.feedparser, "parse", lambda _url: _feed([{
        "title": "Markets rise",
        "summary": "Stocks advance",
        "link": "https://finance.yahoo.com/a",
        "published_parsed": PUBLISHED,
    }]))

    articles = rss_yahoo.load_yahoo_rss()

    assert articles[0]["source"] == "Yahoo Finance"
    assert articles[0]["datetime"] > 0
    assert len(articles[0]["id"]) == 40


def test_marketwatch_topstories_and_symbol_filter(monkeypatch):
    monkeypatch.setattr(rss_marketwatch.feedparser, "parse", lambda _url: _feed([{
        "title": "Apple AAPL reports earnings",
        "summary": "Technology results",
        "link": "https://marketwatch.com/a",
        "published_parsed": PUBLISHED,
    }]))

    assert len(rss_marketwatch.load_marketwatch_topstories()) == 1
    assert len(rss_marketwatch.load_marketwatch_rss("AAPL")) == 1
    assert rss_marketwatch.load_marketwatch_rss("MSFT") == []


def test_thestreet_loader_converts_feed_schema(monkeypatch):
    monkeypatch.setattr(the_street.feedparser, "parse", lambda _url: _feed([{
        "title": "Company earnings",
        "description": "<p>Revenue increased.</p>",
        "link": "https://thestreet.com/a",
        "published_parsed": PUBLISHED,
        "dc_creator": "Reporter",
    }]))

    articles = the_street.load_thestreet_rss()

    assert articles[0]["headline"] == "Company earnings"
    assert articles[0]["summary"] == "Revenue increased."
    assert articles[0]["author"] == "Reporter"
