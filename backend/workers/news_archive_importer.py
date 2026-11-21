import argparse
import json
import logging
from pathlib import Path
from typing import Iterable

from news_sources.gdelt_source import _parse_seen_date
from services.news_indexer import NewsIndexer
from workers.news_worker import (
    EMBEDDER_MODEL,
    VSTORE_PATH,
    NewsWorker,
    _article_exists,
    _normalize_article,
)

logger = logging.getLogger(__name__)


def _iter_json_rows(path: Path) -> Iterable[dict]:
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping invalid JSONL row in %s", path)
                    continue
                if isinstance(row, dict):
                    yield row
        return

    with path.open("r", encoding="utf-8") as fh:
        try:
            payload = json.load(fh)
        except json.JSONDecodeError:
            logger.warning("Skipping invalid JSON file %s", path)
            return

    rows = payload.get("articles") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        logger.warning("Skipping JSON file without an article list: %s", path)
        return
    for row in rows:
        if isinstance(row, dict):
            yield row


def iter_archive_rows(root: str | Path) -> Iterable[dict]:
    root_path = Path(root)
    if root_path.is_file():
        yield from _iter_json_rows(root_path)
        return

    for path in sorted(root_path.rglob("*")):
        if path.suffix.lower() in {".json", ".jsonl"}:
            yield from _iter_json_rows(path)


def normalize_archive_article(row: dict) -> dict:
    source = row.get("domain") or row.get("source") or row.get("sourceCountry") or "GDELT"
    published_ts = row.get("published_ts") or row.get("datetime") or _parse_seen_date(row.get("seendate"))
    return _normalize_article(
        {
            "url": row.get("url"),
            "headline": row.get("headline") or row.get("title"),
            "summary": row.get("summary") or row.get("snippet") or row.get("description"),
            "datetime": published_ts,
            "source": source,
        },
        source,
    )


def import_archive(
    root: str | Path,
    *,
    batch_size: int = 500,
    indexer: NewsIndexer | None = None,
    worker: NewsWorker | None = None,
    skip_existing: bool = True,
) -> dict:
    indexer = indexer or NewsIndexer(path=VSTORE_PATH, model=EMBEDDER_MODEL)
    worker = worker or NewsWorker(indexer=indexer)
    batch: list[dict] = []
    stats = {"seen": 0, "imported": 0, "skipped": 0}

    def flush():
        if not batch:
            return
        indexer.upsert_batch(batch)
        worker._add_recent(batch)
        stats["imported"] += len(batch)
        batch.clear()

    for row in iter_archive_rows(root):
        stats["seen"] += 1
        item = normalize_archive_article(row)
        url = item.get("url") or ""
        if not url:
            stats["skipped"] += 1
            continue
        if skip_existing and _article_exists(url):
            stats["skipped"] += 1
            continue
        batch.append(item)
        if len(batch) >= batch_size:
            flush()

    flush()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Import local GDELT/news JSON or JSONL archives without querying remote APIs.")
    parser.add_argument("path", help="File or directory containing .json/.jsonl article archives")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--include-existing", action="store_true", help="Re-upsert articles even if their URL is already in Redis")
    args = parser.parse_args()

    logging.basicConfig(level="INFO")
    stats = import_archive(
        args.path,
        batch_size=max(1, args.batch_size),
        skip_existing=not args.include_existing,
    )
    logger.info("Archive import complete: %s", stats)


if __name__ == "__main__":
    main()
