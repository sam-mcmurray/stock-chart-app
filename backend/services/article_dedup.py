import hashlib
import re
from copy import deepcopy
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "vero_conv", "vero_id", "oly_anon_id", "oly_enc_id",
}
TRACKING_QUERY_PREFIXES = ("utm_",)


def canonicalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return raw
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return raw
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not key.lower().startswith(TRACKING_QUERY_PREFIXES)
    ))
    return urlunsplit(("https", netloc, path, query, ""))


def _normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def content_fingerprint(article: dict[str, Any]) -> str:
    title = _normalized_text(article.get("title") or article.get("headline") or "")
    if len(title.split()) < 7:
        return ""
    published_ts = int(article.get("published_ts") or article.get("datetime") or 0)
    day_bucket = published_ts // 86400 if published_ts > 0 else 0
    return hashlib.sha1(f"{day_bucket}:{title}".encode("utf-8")).hexdigest()


def article_dedup_id(article: dict[str, Any]) -> str:
    canonical_url = canonicalize_url(article.get("canonical_url") or article.get("url") or "")
    return hashlib.sha1(canonical_url.encode("utf-8")).hexdigest() if canonical_url else ""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)] if value not in (None, "") else []


def _quality(article: dict[str, Any]) -> tuple[int, int, int]:
    summary = article.get("summary") or article.get("description") or ""
    title = article.get("title") or article.get("headline") or ""
    return (bool(article.get("author")), len(summary), len(title))


def merge_articles(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    richer, other = (right, left) if _quality(right) > _quality(left) else (left, right)
    merged = deepcopy(richer)
    canonical_url = canonicalize_url(left.get("canonical_url") or left.get("url") or right.get("url") or "")
    merged["canonical_url"] = canonical_url
    merged["dedup_id"] = left.get("dedup_id") or right.get("dedup_id") or article_dedup_id({"url": canonical_url})
    merged["content_fingerprint"] = left.get("content_fingerprint") or right.get("content_fingerprint") or content_fingerprint(merged)
    merged["providers"] = sorted(set(
        _as_list(left.get("providers")) + _as_list(right.get("providers"))
        + _as_list(left.get("provider")) + _as_list(right.get("provider"))
    ))
    merged["source_urls"] = sorted(set(
        _as_list(left.get("source_urls")) + _as_list(right.get("source_urls"))
        + _as_list(left.get("url")) + _as_list(right.get("url"))
    ))
    merged["event_ids"] = sorted(set(
        _as_list(left.get("event_ids")) + _as_list(right.get("event_ids"))
        + _as_list(left.get("event_id")) + _as_list(right.get("event_id"))
    ))
    merged["tickers"] = sorted(set(_as_list(left.get("tickers")) + _as_list(right.get("tickers"))))
    merged["published_ts"] = max(
        int(left.get("published_ts") or 0),
        int(right.get("published_ts") or 0),
    )
    if other.get("metadata") and not merged.get("metadata"):
        merged["metadata"] = other["metadata"]
    return merged


def deduplicate_articles(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    for original in items:
        item = deepcopy(original)
        item["canonical_url"] = canonicalize_url(item.get("url") or "")
        item["dedup_id"] = item.get("dedup_id") or article_dedup_id(item)
        item["content_fingerprint"] = item.get("content_fingerprint") or content_fingerprint(item)
        key = item["dedup_id"] or f"unkeyed:{len(groups)}"
        fingerprint = item["content_fingerprint"]
        if fingerprint and fingerprint in fingerprints:
            key = fingerprints[fingerprint]
        if key in groups:
            groups[key] = merge_articles(groups[key], item)
        else:
            groups[key] = item
        if fingerprint:
            fingerprints[fingerprint] = key
    return list(groups.values())
