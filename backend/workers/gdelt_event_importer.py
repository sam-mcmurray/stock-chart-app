import argparse
import csv
import hashlib
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from services.article_graph import upsert_article_graph
from services.security_master import resolve_company_tickers

EMBEDDER_MODEL = os.getenv("EMBEDDER_MODEL", "all-MiniLM-L6-v2")
VSTORE_PATH = os.getenv("NEWS_COLLECTION_PATH", "/data/chroma")
COLLECTION_NAME = os.getenv("GDELT_IMPORT_COLLECTION_NAME", "gdelt_events_2")

logger = logging.getLogger(__name__)
# Test/legacy injection point. The Qdrant rebuild leaves this unset and never
# imports the embedded Chroma implementation.
NewsIndexer = None

FINANCE_TERMS = re.compile(
    r"\b(stock|stocks|market|markets|finance|financial|bank|banking|"
    r"investor|investment|wall[ -]?street|earnings|revenue|profit|merger|acquisition|"
    r"ipo|dividend|bond|treasury|currency|forex|crypto|bitcoin|inflation|interest[ -]?rate|"
    r"federal[ -]?reserve|central[ -]?bank|economy|economic|"
    r"oil|crude|semiconductor|tariff|bankruptcy)\b",
    re.IGNORECASE,
)
FINANCE_ACTOR_TERMS = re.compile(
    r"\b(federal[ -]?reserve|central[ -]?bank|treasury|stock[ -]?exchange|bank|banking|"
    r"investor|investment|financial|finance|wall[ -]?street|securities|currency|forex|"
    r"nasdaq|dow[ -]?jones|s&p|bitcoin|crypto)\b",
    re.IGNORECASE,
)

ROOT_EVENT_LABELS = {
    "01": "public statement",
    "02": "appeal",
    "03": "express intent",
    "04": "consultation",
    "05": "diplomatic cooperation",
    "06": "material cooperation",
    "07": "aid",
    "08": "yield",
    "09": "investigation",
    "10": "demand",
    "11": "disapproval",
    "12": "rejection",
    "13": "threat",
    "14": "protest",
    "15": "force posture",
    "16": "reduced relations",
    "17": "coercion",
    "18": "assault",
    "19": "fight",
    "20": "mass violence",
}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _event_timestamp(date_added: str, sql_date: str) -> int:
    raw = date_added if len(date_added or "") in {8, 14} else sql_date
    fmt = "%Y%m%d%H%M%S" if len(raw or "") == 14 else "%Y%m%d"
    try:
        return int(datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return 0


def _layout(row: list[str]) -> dict | None:
    if len(row) >= 61:
        return {"action_name": 52, "country_code": 53, "date_added": 59, "source_url": 60}
    if len(row) >= 58:
        return {"action_name": 50, "country_code": 51, "date_added": 56, "source_url": 57}
    return None


def _headline_from_url(source_url: str) -> str:
    candidates = []
    for segment in urlparse(source_url).path.split("/"):
        text = unquote(segment)
        text = re.sub(r"\.(s?html?|php|aspx?|txt)$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[-_]+", " ", text)
        text = re.sub(r"\b\d{6,}\b", "", text)
        text = re.sub(r"\s+", " ", text).strip(" .")
        word_count = len(re.findall(r"[A-Za-z]{2,}", text))
        if word_count >= 4:
            candidates.append((word_count, len(text), text))
    return max(candidates, default=(0, 0, ""))[2]


def normalize_event_row(row: list[str], *, include_all: bool = False) -> dict | None:
    layout = _layout(row)
    if not layout:
        return None

    source_url = (row[layout["source_url"]] or "").strip()
    if not source_url.startswith(("http://", "https://")):
        return None

    actor1 = (row[6] or row[5] or "Unknown actor").strip()
    actor2 = (row[16] or row[15] or "Unknown actor").strip()
    url_words = unquote(urlparse(source_url).path).replace("-", "_").replace("_", " ")
    actor_text = f"{actor1} {actor2}"
    if not include_all and not (
        FINANCE_TERMS.search(url_words) or FINANCE_ACTOR_TERMS.search(actor_text)
    ):
        return None

    event_id = (row[0] or "").strip()
    if not event_id:
        return None
    event_code = (row[26] or "").strip()
    root_code = (row[28] or event_code[:2]).strip().zfill(2)
    root_label = ROOT_EVENT_LABELS.get(root_code, "event")
    location = (row[layout["action_name"]] or "Unknown location").strip()
    country_code = (row[layout["country_code"]] or "").strip().upper()
    tone = round(_number(row[34]), 4)
    goldstein = round(_number(row[30]), 4)
    published_ts = _event_timestamp(row[layout["date_added"]], row[1])
    domain = urlparse(source_url).netloc.lower() or "GDELT"
    document_date = ((row[layout["date_added"]] or row[1])[:8] or "unknown")
    source_hash = hashlib.sha1(source_url.encode("utf-8")).hexdigest()
    inferred_headline = _headline_from_url(source_url)
    event_title = f"{actor1}: {root_label} involving {actor2}"
    title = inferred_headline or event_title
    summary = (
        f"GDELT event {event_id}: {actor1} recorded a {root_label} event involving "
        f"{actor2} in {location}. Event code {event_code}; tone {tone}; Goldstein scale {goldstein}."
    )
    return {
        "id": f"gdelt:{document_date}:{source_hash}",
        "url": source_url,
        "title": title,
        "summary": summary,
        "text": f"{title}. {event_title}. {summary}",
        "published_ts": published_ts,
        "source": domain,
        "tickers": [],
        "metadata": {
            "gdelt_event_id": event_id,
            "event_code": event_code,
            "root_event_code": root_code,
            "quad_class": _integer(row[29]),
            "goldstein_scale": goldstein,
            "average_tone": tone,
            "mentions": _integer(row[31]),
            "sources": _integer(row[32]),
            "articles": _integer(row[33]),
            "actor1": actor1,
            "actor2": actor2,
            "location": location,
            "country_code": country_code,
            "title_inferred_from_url": bool(inferred_headline),
        },
    }


def _archive_date(path: Path) -> str:
    match = re.match(r"(\d{8})", path.name)
    return match.group(1) if match else ""


def iter_archives(root: str | Path, start_date: str | None = None, end_date: str | None = None):
    root_path = Path(root)
    paths = [root_path] if root_path.is_file() else sorted(root_path.glob("*.export.CSV.zip"))
    for path in paths:
        archive_date = _archive_date(path)
        if start_date and archive_date < start_date:
            continue
        if end_date and archive_date > end_date:
            continue
        yield path


def iter_archive_rows(path: Path):
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not members:
            return
        with archive.open(members[0]) as raw:
            lines = (line.decode("utf-8", errors="replace") for line in raw)
            yield from csv.reader(lines, delimiter="\t")


def _load_state(path: Path | None) -> dict:
    if not path or not path.exists():
        return {"completed_files": [], "current_file": None, "current_row": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"completed_files": [], "current_file": None, "current_row": 0}


def _save_state(path: Path | None, state: dict):
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def build_selected_event_id_index(
    root: str | Path,
    *,
    start_date: str,
    end_date: str,
    index_path: str | Path,
    state_path: str | Path,
) -> tuple[set[str], dict]:
    index_file = Path(index_path)
    checkpoint_path = Path(state_path)
    event_ids = set()
    if index_file.exists():
        event_ids.update(
            line.strip()
            for line in index_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    state = _load_state(checkpoint_path)
    completed = set(state.get("completed_files") or [])
    stats = {
        "target_events": len(event_ids),
        "archives_seen": 0,
        "archives_completed": 0,
        "archives_skipped": 0,
        "rows_seen": 0,
        "selected": 0,
        "new_event_ids": 0,
    }

    for archive in iter_archives(root, start_date=start_date, end_date=end_date):
        if archive.name in completed:
            stats["archives_skipped"] += 1
            continue
        stats["archives_seen"] += 1
        archive_ids = set()
        seen_urls = set()
        for row in iter_archive_rows(archive):
            stats["rows_seen"] += 1
            item = normalize_event_row(row)
            if not item or item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            stats["selected"] += 1
            archive_ids.add(str(item["metadata"]["gdelt_event_id"]))

        new_ids = archive_ids - event_ids
        if new_ids:
            index_file.parent.mkdir(parents=True, exist_ok=True)
            with index_file.open("a", encoding="utf-8") as handle:
                for event_id in sorted(new_ids):
                    handle.write(f"{event_id}\n")
                handle.flush()
                os.fsync(handle.fileno())
            event_ids.update(new_ids)
            stats["new_event_ids"] += len(new_ids)

        completed.add(archive.name)
        state["completed_files"] = sorted(completed)
        state["current_file"] = None
        state["current_row"] = 0
        _save_state(checkpoint_path, state)
        stats["archives_completed"] += 1
        if stats["archives_completed"] % 100 == 0:
            logger.info(
                "GDELT event-ID index %s archives complete; %s target events",
                stats["archives_completed"],
                len(event_ids),
            )

    stats["target_events"] = len(event_ids)
    return event_ids, stats


def import_events(
    root: str | Path,
    *,
    indexer=None,
    batch_size: int = 250,
    start_date: str | None = None,
    end_date: str | None = None,
    max_events: int | None = None,
    include_all: bool = False,
    dry_run: bool = False,
    state_path: str | Path | None = None,
) -> dict:
    checkpoint_path = Path(state_path) if state_path and not dry_run else None
    state = _load_state(checkpoint_path)
    completed = set(state.get("completed_files") or [])
    stats = {
        "archives_seen": 0,
        "archives_completed": 0,
        "rows_seen": 0,
        "matched_rows": 0,
        "selected": 0,
        "imported": 0,
        "skipped": 0,
        "duplicate_urls": 0,
        "malformed": 0,
        "samples": [],
    }
    batch = []

    def flush(archive_name: str, row_number: int):
        nonlocal indexer
        if not batch:
            return
        if not dry_run:
            if indexer is None:
                # Legacy importer only. Keeping this import local prevents the
                # Qdrant archive rebuild from loading embedded Chroma at all.
                indexer_factory = NewsIndexer
                if indexer_factory is None:
                    from services.news_indexer import NewsIndexer as indexer_factory

                indexer = indexer_factory(
                    path=VSTORE_PATH,
                    collection=COLLECTION_NAME,
                    model=EMBEDDER_MODEL,
                )
            indexer.upsert_batch(batch, preserve_existing=True)
            upsert_article_graph(batch)
            stats["imported"] += len(batch)
            state["current_file"] = archive_name
            state["current_row"] = row_number
            _save_state(checkpoint_path, state)
        batch.clear()

    for archive in iter_archives(root, start_date=start_date, end_date=end_date):
        archive_name = archive.name
        if archive_name in completed:
            continue
        stats["archives_seen"] += 1
        seen_urls = set()
        resume_row = state.get("current_row", 0) if state.get("current_file") == archive_name else 0
        last_row = resume_row
        for row_number, row in enumerate(iter_archive_rows(archive), start=1):
            if row_number <= resume_row:
                continue
            last_row = row_number
            stats["rows_seen"] += 1
            if len(row) < 58:
                stats["malformed"] += 1
                continue
            item = normalize_event_row(row, include_all=include_all)
            if not item:
                stats["skipped"] += 1
                continue
            stats["matched_rows"] += 1
            if not dry_run:
                item["tickers"] = resolve_company_tickers(
                    f"{item.get('title') or ''} {item['metadata'].get('actor1') or ''} "
                    f"{item['metadata'].get('actor2') or ''}"
                )
                item["event_id"] = item["metadata"].get("gdelt_event_id")
                item["event_code"] = item["metadata"].get("event_code")
                item["location"] = item["metadata"].get("location")
                item["actor1"] = item["metadata"].get("actor1")
                item["actor2"] = item["metadata"].get("actor2")
            if item["url"] in seen_urls:
                stats["duplicate_urls"] += 1
                continue
            seen_urls.add(item["url"])
            stats["selected"] += 1
            if len(stats["samples"]) < 3:
                stats["samples"].append({
                    "id": item["id"],
                    "title": item["title"],
                    "source": item["source"],
                    "url": item["url"],
                    "published_ts": item["published_ts"],
                })
            batch.append(item)
            if len(batch) >= max(1, batch_size):
                flush(archive_name, row_number)
            if max_events and stats["selected"] >= max_events:
                flush(archive_name, row_number)
                return stats

        flush(archive_name, last_row)
        if not dry_run:
            completed.add(archive_name)
            state["completed_files"] = sorted(completed)
            state["current_file"] = None
            state["current_row"] = 0
            _save_state(checkpoint_path, state)
        stats["archives_completed"] += 1
    return stats


def main():
    parser = argparse.ArgumentParser(description="Stream filtered raw GDELT Event ZIP archives into a separate Chroma collection.")
    parser.add_argument("path", help="Event ZIP file or directory containing *.export.CSV.zip files")
    parser.add_argument("--start-date", help="First archive date in YYYYMMDD format")
    parser.add_argument("--end-date", help="Last archive date in YYYYMMDD format")
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--limit", type=int, help="Stop after selecting this many events")
    parser.add_argument("--include-all", action="store_true", help="Disable finance relevance filtering")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing to Chroma")
    parser.add_argument("--state-path", default="/data/chroma/gdelt_event_import_state.json")
    args = parser.parse_args()
    if not args.dry_run and not args.start_date:
        parser.error("--start-date is required for a real import")

    logging.basicConfig(level="INFO")
    stats = import_events(
        args.path,
        batch_size=max(1, args.batch_size),
        start_date=args.start_date,
        end_date=args.end_date,
        max_events=args.limit,
        include_all=args.include_all,
        dry_run=args.dry_run,
        state_path=args.state_path,
    )
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
