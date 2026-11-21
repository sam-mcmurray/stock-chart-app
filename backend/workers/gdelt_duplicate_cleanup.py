import argparse
import json
import os
from typing import Any

import chromadb


VSTORE_PATH = os.getenv("NEWS_COLLECTION_PATH", "/data/chroma")
NEWS_COLLECTION = os.getenv("NEWS_COLLECTION_NAME", "news")
CANONICAL_COLLECTION = os.getenv("GDELT_IMPORT_COLLECTION_NAME", "gdelt_events_2")
LEGACY_COLLECTION = os.getenv("GDELT_LEGACY_COLLECTION_NAME", "gdelt_events")
GDELT_EXPORT_FILTER = {"provider": {"$eq": "gdelt export"}}


def _collection_names(client) -> set[str]:
    return {
        item.name if hasattr(item, "name") else str(item)
        for item in client.list_collections()
    }


def cleanup_duplicates(client, *, apply: bool = False) -> dict[str, Any]:
    names = _collection_names(client)
    if CANONICAL_COLLECTION not in names:
        raise RuntimeError(
            f"Refusing cleanup because canonical collection {CANONICAL_COLLECTION!r} is missing"
        )
    if len({NEWS_COLLECTION, CANONICAL_COLLECTION, LEGACY_COLLECTION}) != 3:
        raise RuntimeError("News, canonical, and legacy collection names must be distinct")

    news_duplicate_count = 0
    if NEWS_COLLECTION in names:
        news = client.get_collection(NEWS_COLLECTION)
        payload = news.get(where=GDELT_EXPORT_FILTER, include=[])
        news_duplicate_count = len(payload.get("ids") or [])
        if apply and news_duplicate_count:
            news.delete(where=GDELT_EXPORT_FILTER)

    legacy_present = LEGACY_COLLECTION in names
    if apply and legacy_present:
        client.delete_collection(LEGACY_COLLECTION)

    return {
        "applied": apply,
        "canonical_collection": CANONICAL_COLLECTION,
        "news_collection": NEWS_COLLECTION,
        "news_gdelt_export_records": news_duplicate_count,
        "legacy_collection": LEGACY_COLLECTION,
        "legacy_collection_present": legacy_present,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove legacy duplicate GDELT storage")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete matched news records and the legacy collection; default is dry-run",
    )
    args = parser.parse_args()
    result = cleanup_duplicates(chromadb.PersistentClient(path=VSTORE_PATH), apply=args.apply)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
