import argparse
import csv
import json
import logging
import re
import zipfile
from pathlib import Path

from services import persistent_store
from services.qdrant_store import GDELT_EVENT_COLLECTION, get_store
from workers.gdelt_event_importer import _integer, _number

logger = logging.getLogger(__name__)
MAX_QDRANT_SCROLL_PAGE_SIZE = 500


def _qdrant_event_ids(store, *, page_size=MAX_QDRANT_SCROLL_PAGE_SIZE) -> set[str]:
    event_ids = set()
    offset = None
    limit = min(MAX_QDRANT_SCROLL_PAGE_SIZE, max(1, int(page_size)))
    while True:
        points, next_offset = store.scroll_payloads(
            GDELT_EVENT_COLLECTION,
            limit=limit,
            offset=offset,
        )
        event_ids.update(
            str(payload.get("gdelt_event_id"))
            for point in points
            for payload in [point.get("payload") or {}]
            if payload.get("gdelt_event_id")
        )
        if next_offset is None:
            break
        offset = next_offset
    return event_ids


def _load_state(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("completed_files") or [])
    except (OSError, ValueError):
        return set()


def _save_state(path: Path | None, completed: set[str]):
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps({"completed_files": sorted(completed)}, indent=2), encoding="utf-8")
    temporary.replace(path)


def indexed_event_ids() -> set[str]:
    return _qdrant_event_ids(get_store())


def iter_mention_archives(root: str | Path, start_date: str, end_date: str):
    for path in sorted(Path(root).glob("*.mentions.CSV.zip")):
        match = re.match(r"(\d{8})", path.name)
        if match and start_date <= match.group(1) <= end_date:
            yield path


def iter_mention_rows(path: Path):
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not members:
            return
        with archive.open(members[0]) as raw:
            lines = (line.decode("utf-8", errors="replace") for line in raw)
            yield from csv.reader(lines, delimiter="\t")


def normalize_mention(row: list[str], target_ids: set[str]) -> dict | None:
    if len(row) < 16 or row[0] not in target_ids:
        return None
    url = (row[5] or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    return {
        "event_id": row[0],
        "url": url,
        "source": (row[4] or "").strip(),
        "mention_ts": _integer(row[2]),
        "sentence_id": _integer(row[6]),
        "confidence": _number(row[11]),
        "document_tone": _number(row[13]),
    }


def import_mentions(
    root,
    *,
    start_date,
    end_date,
    target_ids=None,
    batch_size=1000,
    dry_run=False,
    state_path=None,
):
    target_ids = target_ids if target_ids is not None else indexed_event_ids()
    checkpoint = Path(state_path) if state_path and not dry_run else None
    completed = _load_state(checkpoint)
    stats = {"target_events": len(target_ids), "archives": 0, "archives_skipped": 0, "rows_seen": 0, "matched": 0, "written": 0}
    batch = []
    seen = set()

    def flush():
        if batch and not dry_run:
            stats["written"] += persistent_store.upsert_gdelt_mentions(batch)
        batch.clear()

    for archive in iter_mention_archives(root, start_date, end_date):
        if archive.name in completed:
            stats["archives_skipped"] += 1
            continue
        stats["archives"] += 1
        for row in iter_mention_rows(archive):
            stats["rows_seen"] += 1
            mention = normalize_mention(row, target_ids)
            if not mention:
                continue
            key = (mention["event_id"], mention["url"])
            if key in seen:
                continue
            seen.add(key)
            stats["matched"] += 1
            batch.append(mention)
            if len(batch) >= batch_size:
                flush()
        flush()
        if checkpoint:
            completed.add(archive.name)
            _save_state(checkpoint, completed)
    flush()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Join GDELT Mentions URLs to indexed historical events")
    parser.add_argument("path")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--state-path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level="INFO")
    print(json.dumps(import_mentions(
        args.path,
        start_date=args.start_date,
        end_date=args.end_date,
        batch_size=max(1, args.batch_size),
        dry_run=args.dry_run,
        state_path=args.state_path,
    ), indent=2))


if __name__ == "__main__":
    main()
