import argparse
import json
import logging

from services.article_graph import upsert_article_graph
from services.article_metadata import enrich_articles
from services.news_indexer import NewsIndexer
from workers.news_worker import (
    EMBEDDER_MODEL,
    H_ART_PREFIX,
    VSTORE_PATH,
    Z_ARTICLES,
    NewsWorker,
    _normalize_article,
    r,
)

logger = logging.getLogger(__name__)


def backfill_recent_metadata(limit: int = 500, batch_size: int = 10, timeout: float = 6) -> dict:
    candidates = []
    for article_id in r.zrevrange(Z_ARTICLES, 0, -1):
        raw = r.hget(f"{H_ART_PREFIX}{article_id}", "json")
        if not raw:
            continue
        try:
            article = json.loads(raw)
        except ValueError:
            continue
        if not str(article.get("provider") or "").startswith("gdelt"):
            continue
        if article.get("metadata_enrichment_attempted"):
            continue
        candidates.append(article)
        if len(candidates) >= max(1, limit):
            break

    indexer = NewsIndexer(path=VSTORE_PATH, model=EMBEDDER_MODEL)
    worker = NewsWorker(indexer)
    stats = {"selected": len(candidates), "updated": 0, "publisher_titles": 0}
    for offset in range(0, len(candidates), max(1, batch_size)):
        source_batch = candidates[offset:offset + batch_size]
        enriched = enrich_articles(source_batch, timeout=timeout)
        normalized = []
        for before, after in zip(source_batch, enriched):
            item = _normalize_article(after, before.get("provider") or "GDELT")
            normalized.append(item)
            if item.get("title") != before.get("title"):
                stats["publisher_titles"] += 1
        indexer.upsert_batch(normalized)
        worker._add_recent(normalized)
        upsert_article_graph(normalized)
        stats["updated"] += len(normalized)
        logger.info("Backfilled %s/%s recent articles", stats["updated"], stats["selected"])
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill publisher metadata for cached GDELT news")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=6)
    args = parser.parse_args()
    logging.basicConfig(level="INFO")
    print(json.dumps(backfill_recent_metadata(args.limit, args.batch_size, args.timeout), indent=2))


if __name__ == "__main__":
    main()
