from services.article_metadata import parse_article_metadata
import pytest


@pytest.mark.parametrize("final_url,rejected", [
    ("https://www.publisher.test/", True),
    ("https://publisher.test", True),
    ("https://publisher.test/?article=123", False),
    ("https://publisher.test/new-story", False),
])
def test_fetch_rejects_article_redirect_to_homepage(monkeypatch, final_url, rejected):
    from types import SimpleNamespace
    from services import article_metadata
    monkeypatch.setattr(article_metadata.requests, "get", lambda *a, **k: SimpleNamespace(
        url=final_url, headers={"content-type": "text/html"},
        text='<meta property="og:title" content="Publisher news">',
        raise_for_status=lambda: None,
    ))
    result = article_metadata.fetch_article_metadata("https://publisher.test/old-story")
    assert bool(result.get("rejected_reason")) is rejected
    assert bool(result.get("title")) is not rejected


def test_fetch_uses_final_article_url_for_heading_corroboration(monkeypatch):
    from types import SimpleNamespace
    from services import article_metadata
    monkeypatch.setattr(article_metadata.requests, "get", lambda *a, **k: SimpleNamespace(
        url="https://publisher.test/trans-border-trade-beyond-project",
        headers={"content-type": "text/html"}, raise_for_status=lambda: None,
        text='<meta property="og:title" content="Other unrelated story">'
             '<h1>Trans-border Trade: Beyond A Project!</h1>',
    ))
    result = article_metadata.fetch_article_metadata("https://publisher.test/123")
    assert result["title"] == "Trans-border Trade: Beyond A Project!"


@pytest.mark.parametrize("url,extra_heading,corrected", [
    ("https://publisher.test/trans-border-trade-beyond-project", "", True),
    ("https://publisher.test/12345", "", False),
    ("https://publisher.test/other-story", "", False),
    ("https://publisher.test/trans-border-trade-beyond-project", "<h1>Another story</h1>", False),
    ("", "", False),
])
def test_visible_heading_requires_url_corroboration(url, extra_heading, corrected):
    result = parse_article_metadata(
        '<meta property="og:title" content="Mercy Ships Operation: Government Expresses Satisfaction">'
        '<meta name="description" content="unrelated site keywords">'
        '<meta name="author" content="Unrelated author">'
        '<meta property="article:published_time" content="2020-01-01T00:00:00Z">'
        '<h1>Trans-border Trade: Beyond A Project!</h1>' + extra_heading,
        url=url,
    )
    assert bool(result["replaced_metadata_title"]) is corrected
    if corrected:
        assert result["title"] == "Trans-border Trade: Beyond A Project!"
        assert result["summary"] == ""
        assert result["author"] == ""
        assert result["published_ts"] == 0
    else:
        assert result["title"].startswith("Mercy Ships")


def test_url_matching_metadata_is_not_overridden_by_another_heading():
    result = parse_article_metadata(
        '<meta property="og:title" content="Trade project expands regional commerce">'
        '<h1>Trans-border Trade: Beyond A Project!</h1>',
        url="https://publisher.test/trans-border-trade-beyond-project",
    )
    assert result["title"] == "Trade project expands regional commerce"
    assert not result["replaced_metadata_title"]


def test_parse_article_metadata_extracts_bounded_article_text():
    result = parse_article_metadata("""
        <html>
          <head>
            <meta property="og:title" content="Acme signs supply agreement">
            <meta name="description" content="Acme selected Beta as its supplier.">
          </head>
          <body>
            <nav>This navigation should not be classified.</nav>
            <article>
              <p>Acme Corporation selected Beta Systems as its primary component supplier.</p>
              <aside><p>This unrelated recommendation should be excluded from the article.</p></aside>
              <p>The agreement begins next quarter and covers the companies' US operations.</p>
              <p>Both businesses said the supply arrangement will support their production plans.</p>
            </article>
          </body>
        </html>
    """)

    assert result["title"] == "Acme signs supply agreement"
    assert "primary component supplier" in result["article_text"]
    assert "agreement begins next quarter" in result["article_text"]
    assert "unrelated recommendation" not in result["article_text"]


def test_parse_article_metadata_rejects_unrelated_template_story():
    result = parse_article_metadata("""
        <html>
          <head><meta property="og:title" content="North Korea arrests alleged bank hackers"></head>
          <body>
            <article>
              <p>Wise resubmitted its United States banking charter application this week.</p>
              <p>The application follows recently adopted payments legislation.</p>
            </article>
          </body>
        </html>
    """)

    assert result["article_text"] == ""
