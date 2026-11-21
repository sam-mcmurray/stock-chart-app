from services import news_article_resolver
from unittest.mock import Mock


def test_full_publisher_content_never_queries_mentions(monkeypatch):
    mentions = Mock(side_effect=AssertionError("unneeded mention lookup"))
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(news_article_resolver.persistent_store, "list_gdelt_mentions", mentions)
    monkeypatch.setattr(news_article_resolver, "fetch_article_metadata", lambda _: {
        "title": "Company announces new factory investment",
        "article_text": "The company announced a new factory investment.",
    })
    result = news_article_resolver.resolve_article_context({
        "url": "https://publisher.test/story", "title_inferred": True, "event_ids": ["1", "2"],
    })
    assert result["content_resolution_source"] == "canonical"
    mentions.assert_not_called()


def test_mentions_stop_after_first_full_fallback(monkeypatch):
    mentions = Mock(return_value=[{"url": "https://backup.test/story"}])
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(news_article_resolver.persistent_store, "list_gdelt_mentions", mentions)
    monkeypatch.setattr(news_article_resolver, "fetch_article_metadata", lambda url: {} if "publisher" in url else {
        "title": "Company announces new factory investment", "article_text": "Factory investment details.",
    })
    result = news_article_resolver.resolve_article_context({
        "url": "https://publisher.test/story", "title_inferred": True, "event_ids": ["1", "2"],
    })
    assert result["content_resolution_source"] == "gdelt_mention"
    mentions.assert_called_once_with("1", limit=3)


def test_candidate_limit_and_duplicates_do_not_trigger_extra_queries(monkeypatch):
    mentions = Mock(side_effect=lambda event, **_: [
        {"url": "https://publisher.test/story"}, {"url": f"https://backup.test/{event}"},
    ])
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(news_article_resolver.persistent_store, "list_gdelt_mentions", mentions)
    candidates = list(news_article_resolver._candidate_urls({
        "url": "https://publisher.test/story", "source_urls": ["https://publisher.test/story"],
        "event_ids": ["1", "1", "2", "3", "4", "5"],
    }))
    assert len(candidates) == 5
    assert [call.args[0] for call in mentions.call_args_list] == ["1", "2", "3", "4"]
    assert candidates[-1] == ("https://backup.test/4", "gdelt_mention")


def test_five_source_urls_do_not_query_mentions(monkeypatch):
    mentions = Mock(side_effect=AssertionError("candidate cap already reached"))
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(news_article_resolver.persistent_store, "list_gdelt_mentions", mentions)
    candidates = list(news_article_resolver._candidate_urls({
        "source_urls": [f"https://publisher.test/{i}" for i in range(8)], "event_ids": ["1"],
    }))
    assert len(candidates) == 5
    mentions.assert_not_called()


def test_corrected_template_headline_discards_contaminated_context(monkeypatch):
    old = "Mercy Ships Operation: Government Expresses Satisfaction"
    title = "Trans-border Trade: Beyond A Project!"
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: False)
    monkeypatch.setattr(news_article_resolver, "fetch_article_metadata", lambda url: {
        "title": title, "replaced_metadata_title": old, "summary": "", "tickers": [],
    })
    result = news_article_resolver.resolve_article_context({
        "title": old, "url": "https://publisher.test/trans-border-trade-beyond-project",
        "summary": "Unrelated keywords", "article_text": "Old unrelated body",
        "publisher_tickers": ["WRONG"],
    })
    assert result["title"] == title
    assert result["summary"] == title
    assert "article_text" not in result
    assert result["publisher_tickers"] == []
    assert result["content_resolution_source"] == "canonical_metadata_only"


def test_good_publisher_headline_is_still_verified(monkeypatch):
    monkeypatch.setattr(
        news_article_resolver,
        "fetch_article_metadata",
        lambda _url: {
            "title": "Microsoft reports stronger cloud revenue",
            "summary": "Cloud revenue increased in the quarter.",
            "article_text": "Microsoft reported stronger cloud revenue in the quarter.",
            "tickers": ["MSFT"],
        },
    )

    result = news_article_resolver.resolve_article_context({
        "title": "Microsoft reports stronger cloud revenue",
        "url": "https://publisher.test/story",
    })

    assert result["title"] == "Microsoft reports stronger cloud revenue"
    assert result["headline_resolution_source"] == "canonical"
    assert result["content_verified"] is True
    assert result["publisher_tickers"] == ["MSFT"]
    assert "cloud revenue" in result["article_text"]


def test_failed_canonical_uses_ranked_gdelt_mention(monkeypatch):
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(
        news_article_resolver.persistent_store,
        "list_gdelt_mentions",
        lambda _event_id, limit=10: [{"url": "https://alternate.test/story"}],
    )
    monkeypatch.setattr(
        news_article_resolver,
        "fetch_article_metadata",
        lambda url: {} if "broken" in url else {
            "title": "Verified publisher headline",
            "summary": "Verified publisher summary",
        },
    )

    result = news_article_resolver.resolve_article_context({
        "title": "bad extracted headline here",
        "title_inferred": True,
        "url": "https://broken.test/story",
        "event_ids": ["123"],
    })

    assert result["title"] == "Verified publisher headline"
    assert result["content_verified"] is True
    assert result["headline_resolution_source"] == "gdelt_mention"


def test_gated_canonical_uses_mention_for_article_substance(monkeypatch):
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: True)
    monkeypatch.setattr(
        news_article_resolver.persistent_store,
        "list_gdelt_mentions",
        lambda _event_id, limit=10: [{"url": "https://coverage.test/story"}],
    )

    def metadata(url):
        if "publisher" in url:
            return {
                "title": "Nvidia selects Luminar for autonomous vehicle platform",
                "summary": "The publisher page is subscriber-only.",
                "article_text": "",
                "tickers": ["NVDA"],
            }
        return {
            "title": "Luminar joins Nvidia autonomous vehicle project",
            "summary": "The companies announced a lidar supply relationship.",
            "article_text": (
                "Luminar Technologies will supply lidar components for Nvidia's "
                "new autonomous vehicle platform under a multi-year agreement."
            ),
            "tickers": ["LAZR"],
        }

    monkeypatch.setattr(news_article_resolver, "fetch_article_metadata", metadata)

    result = news_article_resolver.resolve_article_context({
        "title": "Nvidia selects Luminar for autonomous vehicle platform",
        "summary": "Nvidia selected Luminar for a new vehicle platform.",
        "url": "https://publisher.test/story",
        "event_ids": ["123"],
    })

    assert result["title"] == "Nvidia selects Luminar for autonomous vehicle platform"
    assert result["headline_resolution_source"] == "canonical"
    assert result["content_resolution_source"] == "gdelt_mention"
    assert result["resolved_url"] == "https://publisher.test/story"
    assert result["supporting_url"] == "https://coverage.test/story"
    assert result["publisher_tickers"] == ["LAZR", "NVDA"]
    assert "multi-year agreement" in result["article_text"]


def test_retry_clears_homepage_content_and_uses_mention(monkeypatch):
    item = {"title": "Publisher news", "original_title": "Publisher news",
            "summary": "Site-wide description", "article_text": "Unrelated text",
            "url": "https://publisher.test/story", "resolved_url": "https://publisher.test/story",
            "content_verified": True, "symbols": ["US"], "publisher_tickers": ["BANK"]}
    monkeypatch.setattr(news_article_resolver, "_candidate_urls", lambda _: [
        (item["url"], "canonical"), ("https://coverage.test/story", "gdelt_mention")])
    monkeypatch.setattr(news_article_resolver, "fetch_article_metadata", lambda url:
        {"rejected_reason": "article_redirect_to_homepage"} if url == item["url"] else
        {"title": "Port discusses new investment", "summary": "Delegates discussed port investment.",
         "article_text": "Delegates discussed port investment."})
    result = news_article_resolver.resolve_article_context(item)
    assert result["content_verified"] is True
    assert result["headline_resolution_source"] == "gdelt_mention"
    assert result["title"] == "Port discusses new investment"
    assert result["publisher_tickers"] == []
    assert result["symbols"] == []
    assert "Unrelated" not in result["article_text"]


def test_failed_retry_does_not_retain_verified_status(monkeypatch):
    monkeypatch.setattr(news_article_resolver, "_candidate_urls", lambda _: [("https://publisher.test/story", "canonical")])
    monkeypatch.setattr(news_article_resolver, "fetch_article_metadata", lambda _: {})
    result = news_article_resolver.resolve_article_context({
        "title": "Original article title", "content_verified": True,
        "content_resolution_source": "canonical", "headline_resolution_source": "canonical"})
    assert result["content_verified"] is False
    assert result["headline_resolution_source"] == "headline_only"
    assert result["content_resolution_source"] == "headline_summary"


def test_homepage_retry_without_mentions_stays_unverified(monkeypatch):
    url = "https://publisher.test/removed-story"
    monkeypatch.setattr(news_article_resolver, "_candidate_urls", lambda _: [(url, "canonical")])
    monkeypatch.setattr(news_article_resolver, "fetch_article_metadata", lambda _: {
        "rejected_reason": "article_redirect_to_homepage"})
    result = news_article_resolver.resolve_article_context({
        "url": url, "resolved_url": url, "title": "Publisher news", "original_title": "Publisher news",
        "content_verified": True, "article_text": "Homepage content", "summary": "Homepage description",
        "publisher_tickers": ["BANK"], "symbols": ["US"]})
    assert result["title"] == url
    assert result["title_inferred"] is True
    assert result["content_verified"] is False
    assert result["headline_resolution_source"] == "headline_only"
    assert result["summary"] == ""
    assert "article_text" not in result
    assert "resolved_url" not in result
    assert result["publisher_tickers"] == result["symbols"] == []


def test_unrelated_redirect_page_is_not_treated_as_verified(monkeypatch):
    monkeypatch.setattr(news_article_resolver.persistent_store, "enabled", lambda: False)
    monkeypatch.setattr(
        news_article_resolver,
        "fetch_article_metadata",
        lambda _url: {
            "title": "Publisher home page and latest stories",
            "summary": "Browse the latest coverage.",
            "article_text": "Latest unrelated stories from across the publisher.",
        },
    )

    result = news_article_resolver.resolve_article_context({
        "title": "Microsoft reports stronger cloud revenue",
        "url": "https://publisher.test/removed-story",
    })

    assert result["title"] == "Microsoft reports stronger cloud revenue"
    assert result["content_verified"] is False
    assert result["headline_resolution_source"] == "headline_only"
