import json
import logging
import re
import time
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

import requests
from bs4 import BeautifulSoup
from services.ticker_utils import is_plausible_ticker
from services.security_master import resolve_company_tickers

logger = logging.getLogger(__name__)
MAX_FUTURE_PUBLISH_SKEW_SECONDS = 15 * 60
MAX_ARTICLE_TEXT_CHARS = 12_000
MIN_ARTICLE_TEXT_CHARS = 120
ARTICLE_MATCH_STOPWORDS = {
    "about", "after", "against", "from", "into", "over", "that", "their",
    "this", "with", "would", "news", "report", "reports",
}


def _meta_content(soup: BeautifulSoup, *selectors: tuple[str, str]) -> str:
    for attribute, value in selectors:
        node = soup.find("meta", attrs={attribute: value})
        if node and node.get("content"):
            return node["content"].strip()
    return ""


def _json_ld_values(value, key: str):
    if isinstance(value, dict):
        if value.get(key):
            yield value[key]
        for child in value.values():
            yield from _json_ld_values(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from _json_ld_values(child, key)


def _parse_timestamp(value) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return 0


def _match_terms(value: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(term) >= 4 and term not in ARTICLE_MATCH_STOPWORDS
    }


def _article_text(soup: BeautifulSoup, title: str, summary: str) -> str:
    candidates = soup.find_all("article") + soup.find_all("main")
    title_terms = _match_terms(title)
    summary_terms = _match_terms(summary)
    best_text = ""
    best_score = -1
    for candidate in candidates:
        root = BeautifulSoup(str(candidate), "html.parser")
        for node in root.find_all(
            ["script", "style", "nav", "aside", "footer", "form", "noscript", "svg"]
        ):
            node.decompose()
        paragraphs = []
        seen_paragraphs = set()
        for node in root.find_all(["p", "h2", "h3"]):
            text = " ".join(node.get_text(" ", strip=True).split())
            fingerprint = text.lower()
            if len(text) >= 30 and fingerprint not in seen_paragraphs:
                paragraphs.append(text)
                seen_paragraphs.add(fingerprint)
        article_text = "\n".join(paragraphs)[:MAX_ARTICLE_TEXT_CHARS]
        if len(article_text) < MIN_ARTICLE_TEXT_CHARS:
            continue
        article_terms = _match_terms(article_text)
        title_overlap = len(title_terms.intersection(article_terms))
        summary_overlap = len(summary_terms.intersection(article_terms))
        if title_terms and title_overlap < min(2, len(title_terms)):
            continue
        score = (title_overlap * 3) + summary_overlap
        if score > best_score:
            best_text = article_text
            best_score = score
    return best_text


def parse_article_metadata(html: str, *, url: str = "") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    json_ld = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            json_ld.append(json.loads(node.string or node.get_text() or ""))
        except (TypeError, json.JSONDecodeError):
            continue

    headline = next(_json_ld_values(json_ld, "headline"), "")
    title = unescape(str(headline).strip()) if headline else ""
    if not title:
        title = _meta_content(
        soup,
        ("property", "og:title"),
        ("name", "twitter:title"),
        )
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = unescape(title)

    summary = _meta_content(
        soup,
        ("property", "og:description"),
        ("name", "description"),
        ("name", "twitter:description"),
    )
    if not summary:
        description = next(_json_ld_values(json_ld, "description"), "")
        summary = str(description).strip() if description else ""
    summary = unescape(summary)
    replaced_metadata_title = ""
    headings = soup.find_all("h1")
    if url and len(headings) == 1:
        visible_title = unescape(headings[0].get_text(" ", strip=True))
        visible_terms = _match_terms(visible_title)
        slug_terms = _match_terms(unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]))
        # Only override contradictory metadata when the URL corroborates a
        # unique visible article heading. Numeric URLs provide no such evidence.
        if (
            len(visible_terms) >= 3
            and len(visible_terms & slug_terms) >= 3
            and len(visible_terms & slug_terms) / len(visible_terms) >= 0.60
            and not (_match_terms(title) & slug_terms)
            and title
        ):
            replaced_metadata_title = title
            title = visible_title
            summary = ""  # Description may belong to the same unrelated template.

    published = _meta_content(
        soup,
        ("property", "article:published_time"),
        ("name", "date"),
        ("itemprop", "datePublished"),
    ) or next(_json_ld_values(json_ld, "datePublished"), "")

    author = _meta_content(soup, ("name", "author"))
    if not author:
        author_value = next(_json_ld_values(json_ld, "author"), "")
        if isinstance(author_value, dict):
            author = str(author_value.get("name") or "").strip()
        elif isinstance(author_value, list):
            author = ", ".join(
                str(item.get("name") if isinstance(item, dict) else item).strip()
                for item in author_value
                if item
            )
        elif author_value:
            author = str(author_value).strip()

    location = ""
    location_value = next(_json_ld_values(json_ld, "contentLocation"), "")
    if not location_value:
        location_value = next(_json_ld_values(json_ld, "locationCreated"), "")
    if isinstance(location_value, dict):
        address = location_value.get("address") or {}
        parts = [
            location_value.get("name"),
            address.get("addressRegion") if isinstance(address, dict) else None,
            address.get("addressCountry") if isinstance(address, dict) else None,
        ]
        location = ", ".join(str(part).strip() for part in parts if part)
    elif location_value:
        location = str(location_value).strip()

    ticker_candidates = set()
    for value in _json_ld_values(json_ld, "tickerSymbol"):
        values = value if isinstance(value, list) else [value]
        ticker_candidates.update(str(item).upper() for item in values if item)
    article_root = soup.find("article") or soup.find("main") or soup
    for attribute in ("data-ticker", "data-symbol"):
        ticker_candidates.update(
            str(node.get(attribute) or "").upper()
            for node in article_root.find_all(attrs={attribute: True})
        )
    visible_text = article_root.get_text(" ", strip=True)
    ticker_candidates.update(re.findall(
        r"\b(?:NASDAQ|NYSE|NYSEAMERICAN|AMEX|OTCMKTS|OTCQX|OTCQB)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\b",
        visible_text,
    ))
    tickers = sorted(symbol for symbol in ticker_candidates if is_plausible_ticker(symbol))

    return {
        "title": title,
        "replaced_metadata_title": replaced_metadata_title,
        "summary": summary,
        "article_text": _article_text(soup, title, summary),
        "published_ts": 0 if replaced_metadata_title else _parse_timestamp(published),
        "author": "" if replaced_metadata_title else author,
        "tickers": [] if replaced_metadata_title else tickers,
        "location": "" if replaced_metadata_title else location,
    }


def fetch_article_metadata(url: str, timeout: float = 6) -> dict:
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "StockChartResearch/1.0"},
        )
        response.raise_for_status()
        final_url = getattr(response, "url", None) or url
        requested_path = urlsplit(url).path.rstrip("/")
        final_path = urlsplit(final_url).path.rstrip("/")
        if requested_path and not final_path and not urlsplit(final_url).query:
            return {"rejected_reason": "article_redirect_to_homepage"}
        if "html" not in response.headers.get("content-type", "text/html").lower():
            return {}
        return parse_article_metadata(response.text[:1_000_000], url=final_url)
    except Exception as exc:
        logger.info("Publisher metadata unavailable for %s: %s", url, exc)
        return {}


def enrich_article(article: dict, timeout: float = 6) -> dict:
    enriched = dict(article)
    metadata = fetch_article_metadata(str(article.get("url") or ""), timeout=timeout)
    enriched["metadata_enrichment_attempted"] = True
    if not metadata.get("title"):
        return enriched
    enriched["gdelt_summary"] = article.get("gdelt_summary") or article.get("summary") or ""
    enriched["title"] = metadata["title"]
    enriched["headline"] = metadata["title"]
    if metadata.get("replaced_metadata_title"):
        enriched["summary"] = metadata["title"]
        enriched["tickers"] = []
    if metadata.get("summary"):
        enriched["summary"] = metadata["summary"]
    if metadata.get("published_ts"):
        publisher_ts = int(metadata["published_ts"])
        if publisher_ts <= int(time.time()) + MAX_FUTURE_PUBLISH_SKEW_SECONDS:
            enriched["published_ts"] = publisher_ts
            enriched["datetime"] = publisher_ts
        else:
            logger.info(
                "Ignoring future publisher timestamp for %s: %s",
                article.get("url"),
                publisher_ts,
            )
    if metadata.get("author"):
        enriched["author"] = metadata["author"]
    if metadata.get("location"):
        enriched["location"] = metadata["location"]
    if metadata.get("tickers"):
        enriched["tickers"] = metadata["tickers"]
    graph_tickers = resolve_company_tickers(
        f"{metadata.get('title') or ''} {metadata.get('summary') or ''}"
    )
    if graph_tickers:
        enriched["tickers"] = sorted(set(enriched.get("tickers") or []).union(graph_tickers))
    enriched["title_inferred"] = False
    return enriched


def enrich_articles(articles: list[dict], *, timeout: float = 6, workers: int = 6) -> list[dict]:
    if not articles:
        return []
    results = [None] * len(articles)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(articles)))) as executor:
        futures = {
            executor.submit(enrich_article, article, timeout): index
            for index, article in enumerate(articles)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception:
                logger.exception("Publisher metadata enrichment failed")
                results[index] = articles[index]
    return results
