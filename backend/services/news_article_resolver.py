import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from services import persistent_store
from services.article_dedup import canonicalize_url
from services.article_metadata import fetch_article_metadata


def headline_needs_resolution(item: dict) -> bool:
    title = str(item.get("title") or "").strip()
    words = title.split()
    return (
        bool(item.get("title_inferred"))
        or len(words) < 4
        or title.startswith(("http://", "https://", "GDELT event "))
        or "_" in title
        or (title == title.lower() and len(words) >= 4)
        or bool(re.search(r"\b(?:html?|php|aspx?)\b$", title, re.IGNORECASE))
    )


def _headline_terms(value: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(term) >= 4
    }


def _headlines_match(expected: str, fetched: str) -> bool:
    expected_terms = _headline_terms(expected)
    fetched_terms = _headline_terms(fetched)
    if not expected_terms or not fetched_terms:
        return False
    overlap = len(expected_terms.intersection(fetched_terms))
    return overlap >= min(3, len(expected_terms)) or (
        overlap / min(len(expected_terms), len(fetched_terms))
    ) >= 0.60


def _coverage_matches(item: dict, metadata: dict) -> bool:
    if headline_needs_resolution(item):
        return True
    reference_terms = _headline_terms(
        f"{item.get('title') or ''} {item.get('summary') or ''}"
    )
    coverage_terms = _headline_terms(
        f"{metadata.get('title') or ''} {metadata.get('summary') or ''} "
        f"{(metadata.get('article_text') or '')[:2000]}"
    )
    if not reference_terms or not coverage_terms:
        return False
    overlap = len(reference_terms.intersection(coverage_terms))
    return overlap >= 3 or overlap / min(len(reference_terms), len(coverage_terms)) >= 0.40


def _candidate_urls(item: dict) -> Iterator[tuple[str, str]]:
    """Fetch mention candidates only after earlier sources have been tried."""
    seen = set()
    for url in [item.get("url"), *(item.get("source_urls") or [])]:
        identity = canonicalize_url(str(url or ""))
        if url and identity not in seen:
            seen.add(identity)
            yield str(url), "canonical"
            if len(seen) >= 5:
                return
    if not persistent_store.enabled():
        return
    seen_events = set()
    for event_id in item.get("event_ids") or []:
        if str(event_id) in seen_events:
            continue
        seen_events.add(str(event_id))
        for mention in persistent_store.list_gdelt_mentions(str(event_id), limit=3):
            url = str(mention.get("url") or "")
            identity = canonicalize_url(url)
            if url and identity not in seen:
                seen.add(identity)
                yield url, "gdelt_mention"
                if len(seen) >= 5:
                    return


def resolve_article_context(item: dict) -> dict:
    resolved = dict(item)
    resolved["original_title"] = item.get("title") or ""
    resolved["content_verified"] = False
    resolved["headline_resolution_source"] = "feed"
    resolved["content_resolution_source"] = "headline_summary"
    canonical_found = False
    mention_found = False
    for url, source in _candidate_urls(item):
        metadata = fetch_article_metadata(url)
        if metadata.get("rejected_reason") and source == "canonical" and not canonical_found:
            resolved["resolution_error"] = metadata["rejected_reason"]
            if item.get("content_verified") and item.get("resolved_url") == url:
                # A retry must not reuse content previously attributed to a homepage.
                resolved.pop("article_text", None)
                resolved.pop("resolved_url", None)
                resolved.pop("supporting_url", None)
                resolved["summary"] = ""
                resolved["publisher_tickers"] = []
                resolved["symbols"] = []
                original = str(item.get("original_title") or "")
                resolved["title"] = original if original and original != item.get("title") else url
                resolved["original_title"] = resolved["title"]
                resolved["title_inferred"] = True
        if not metadata.get("title"):
            continue
        if source == "canonical":
            if (
                not headline_needs_resolution(item)
                and not _headlines_match(resolved["original_title"], metadata["title"])
                and resolved["original_title"] != metadata.get("replaced_metadata_title")
            ):
                continue
            if metadata.get("replaced_metadata_title"):
                resolved.pop("article_text", None)
                resolved["publisher_tickers"] = []
            resolved["title"] = metadata["title"]
            resolved["summary"] = (
                metadata.get("summary") or metadata["title"]
                if metadata.get("replaced_metadata_title")
                else metadata.get("summary") or resolved.get("summary") or ""
            )
            resolved["publisher_tickers"] = sorted(set(
                (resolved.get("publisher_tickers") or []) + (metadata.get("tickers") or [])
            ))
            resolved["resolved_url"] = url
            resolved["content_verified"] = True
            resolved["headline_resolution_source"] = "canonical"
            canonical_found = True
            if metadata.get("article_text"):
                resolved["article_text"] = metadata["article_text"]
                resolved["content_resolution_source"] = "canonical"
                return resolved
            resolved["content_resolution_source"] = "canonical_metadata_only"
            continue

        if not _coverage_matches(resolved if resolved.get("resolution_error") else item, metadata):
            continue
        if not canonical_found:
            resolved["title"] = metadata["title"]
            resolved["summary"] = metadata.get("summary") or resolved.get("summary") or ""
            resolved["resolved_url"] = url
            resolved["content_verified"] = True
            resolved["headline_resolution_source"] = "gdelt_mention"
            resolved["content_resolution_source"] = "gdelt_mention_metadata_only"
            mention_found = True
        resolved["publisher_tickers"] = sorted(set(
            (resolved.get("publisher_tickers") or []) + (metadata.get("tickers") or [])
        ))
        if metadata.get("article_text"):
            resolved["article_text"] = metadata["article_text"]
            resolved["content_resolution_source"] = "gdelt_mention"
            resolved["supporting_url"] = url
            return resolved
    if canonical_found or mention_found:
        return resolved
    resolved["headline_resolution_source"] = "headline_only"
    return resolved


def resolve_article_batch(items: list[dict], workers: int = 4) -> list[dict]:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as executor:
        return list(executor.map(resolve_article_context, items))
