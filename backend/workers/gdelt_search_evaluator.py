import argparse
import json
import os
import re
import ssl
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

BENCHMARKS = [
    ("earnings revenue profit guidance", r"earnings?|revenue|profit|guidance|sales"),
    ("merger acquisition takeover deal", r"merg(?:er|e)|acquisition|takeover|deal"),
    ("IPO public offering stock market debut", r"\bipo\b|public offering|market debut|stock listing"),
    ("Federal Reserve interest rates inflation", r"federal reserve|interest rates?|inflation|\bfed\b"),
    ("bankruptcy restructuring debt default", r"bankruptcy|restructur|debt|default"),
    ("semiconductor chip company investment", r"semiconductor|microchip|chipmaker|\bchips?\b"),
    ("oil crude OPEC energy market", r"\boil\b|crude|opec|energy market"),
    ("bank financial results lending", r"\bbanks?\b|financial results?|lending|loans?"),
]


def evaluate_hits(hits, relevance_pattern):
    pattern = re.compile(relevance_pattern, re.IGNORECASE)
    relevant = []
    seen_urls = set()
    duplicate_count = 0
    domains = set()
    for hit in hits:
        text = f"{hit.get('title', '')} {hit.get('summary', '')}"
        relevant.append(bool(pattern.search(text)))
        url = hit.get("url") or ""
        if url in seen_urls:
            duplicate_count += 1
        elif url:
            seen_urls.add(url)
        if hit.get("source"):
            domains.add(hit["source"])
    first_relevant = next((index + 1 for index, value in enumerate(relevant) if value), None)
    return {
        "precision": round(sum(relevant) / max(len(relevant), 1), 3),
        "reciprocal_rank": round(1 / first_relevant, 3) if first_relevant else 0.0,
        "relevant": sum(relevant),
        "returned": len(hits),
        "duplicate_urls": duplicate_count,
        "unique_domains": len(domains),
    }


def search_api(api_base, query, limit, timeout=120, insecure=False):
    url = f"{api_base.rstrip('/')}/rag/events/search?{urlencode({'q': query, 'limit': limit})}"
    context = ssl._create_unverified_context() if insecure and url.lower().startswith("https://") else None
    try:
        with urlopen(url, timeout=timeout, context=context) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"GDELT search request failed for {query!r}: {exc}") from exc
    return int(payload.get("collection_count") or 0), payload.get("hits") or []


def run_benchmark(limit=10, search=None):
    if search is None:
        api_base = os.getenv("GDELT_SEARCH_API_BASE", "https://127.0.0.1:8443")
        search = lambda query, result_limit: search_api(api_base, query, result_limit)
    rows = []
    collection_count = 0
    for query, pattern in BENCHMARKS:
        collection_count, hits = search(query, limit)
        rows.append({"query": query, **evaluate_hits(hits, pattern)})
    return {
        "method": "heuristic lexical relevance audit over semantic results",
        "collection_count": collection_count,
        "queries": len(rows),
        "limit": limit,
        "mean_precision": round(mean(row["precision"] for row in rows), 3),
        "mean_reciprocal_rank": round(mean(row["reciprocal_rank"] for row in rows), 3),
        "duplicate_urls": sum(row["duplicate_urls"] for row in rows),
        "mean_unique_domains": round(mean(row["unique_domains"] for row in rows), 2),
        "results": rows,
    }


def main():
    parser = argparse.ArgumentParser(description="Audit GDELT semantic search quality through the running API")
    parser.add_argument("--limit", type=int, default=10, choices=range(1, 26))
    parser.add_argument(
        "--api-base",
        default=os.getenv("GDELT_SEARCH_API_BASE", "https://127.0.0.1:8443"),
        help="Running stock app API base URL",
    )
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Allow the local development API's self-signed TLS certificate",
    )
    args = parser.parse_args()
    search = lambda query, result_limit: search_api(
        args.api_base,
        query,
        result_limit,
        args.timeout,
        args.insecure,
    )
    print(json.dumps(run_benchmark(args.limit, search=search), indent=2))


if __name__ == "__main__":
    main()
