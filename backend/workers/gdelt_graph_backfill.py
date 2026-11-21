import argparse
import json
import logging
from pathlib import Path

import chromadb

from services.article_graph import upsert_article_graph
from services.security_master import resolve_company_tickers
from workers.gdelt_event_importer import VSTORE_PATH

logger = logging.getLogger(__name__)


def backfill_graph(*, batch_size: int = 500, state_path: str, reset: bool = False) -> dict:
    path = Path(state_path)
    state = {"offset": 0}
    if path.exists() and not reset:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    collection = chromadb.PersistentClient(path=VSTORE_PATH).get_collection("gdelt_events")
    total = collection.count()
    offset = min(int(state.get("offset") or 0), total)
    stats = {"total": total, "start_offset": offset, "processed": 0, "linked": 0}

    while offset < total:
        payload = collection.get(
            limit=max(1, batch_size),
            offset=offset,
            include=["documents", "metadatas"],
        )
        ids = payload.get("ids") or []
        if not ids:
            break
        articles = []
        updated_metadata = []
        for index, metadata in enumerate(payload.get("metadatas") or []):
            meta = dict(metadata or {})
            document = (payload.get("documents") or [""])[index] or ""
            tickers = resolve_company_tickers(
                f"{meta.get('title') or ''} {meta.get('actor1') or ''} {meta.get('actor2') or ''}"
            )
            if tickers:
                meta["tickers"] = tickers
            updated_metadata.append(meta)
            articles.append({
                "url": meta.get("url"),
                "title": meta.get("title"),
                "summary": document,
                "published_ts": meta.get("published_ts"),
                "source": meta.get("source"),
                "provider": "gdelt historical",
                "tickers": tickers,
                "event_id": meta.get("gdelt_event_id"),
                "event_code": meta.get("event_code"),
                "location": meta.get("location"),
                "actor1": meta.get("actor1"),
                "actor2": meta.get("actor2"),
            })
        collection.update(ids=ids, metadatas=updated_metadata)
        stats["linked"] += upsert_article_graph(articles)
        offset += len(ids)
        stats["processed"] += len(ids)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"offset": offset, "total_at_checkpoint": total}, indent=2), encoding="utf-8")
        logger.info("Historical graph backfill %s/%s", offset, total)
    stats["end_offset"] = offset
    return stats


def main():
    parser = argparse.ArgumentParser(description="Link existing historical GDELT events into Neo4j")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--state-path", default="/data/chroma/gdelt_graph_backfill_state.json")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level="INFO")
    print(json.dumps(backfill_graph(
        batch_size=args.batch_size,
        state_path=args.state_path,
        reset=args.reset,
    ), indent=2))


if __name__ == "__main__":
    main()
