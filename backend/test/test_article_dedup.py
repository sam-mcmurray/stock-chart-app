from services.article_dedup import canonicalize_url, deduplicate_articles


def test_canonicalize_url_removes_tracking_and_normalizes_host_scheme():
    assert canonicalize_url("http://WWW.Example.com/story/?utm_source=x&b=2&a=1#top") == (
        "https://example.com/story?a=1&b=2"
    )


def test_deduplicate_articles_preserves_provenance_and_richest_copy():
    items = [
        {
            "url": "https://www.example.com/story?utm_source=gdelt",
            "title": "Company reports record quarterly earnings after market close",
            "summary": "Short.",
            "provider": "gdelt export",
            "event_id": "123",
            "published_ts": 100,
            "tickers": ["ACME"],
        },
        {
            "url": "http://example.com/story",
            "title": "Company reports record quarterly earnings after market close",
            "summary": "A substantially richer publisher summary with financial details.",
            "provider": "finnhub",
            "published_ts": 110,
            "tickers": ["ACME"],
        },
    ]

    result = deduplicate_articles(items)

    assert len(result) == 1
    assert result[0]["summary"].startswith("A substantially richer")
    assert result[0]["providers"] == ["finnhub", "gdelt export"]
    assert result[0]["event_ids"] == ["123"]
    assert len(result[0]["source_urls"]) == 2
