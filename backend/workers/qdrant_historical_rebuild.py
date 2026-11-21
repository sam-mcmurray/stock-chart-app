import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.article_graph import list_qdrant_rebuild_articles
from services.qdrant_store import GDELT_EVENT_COLLECTION, NEWS_COLLECTION, QdrantStore
from workers.gdelt_event_importer import iter_archive_rows, iter_archives, normalize_event_row


logger = logging.getLogger(__name__)
STATE_PATH = Path(os.getenv("QDRANT_REBUILD_STATE_PATH", "/data/migration/rebuild-state.json"))
BATCH_SIZE = max(1, int(os.getenv("QDRANT_REBUILD_BATCH_SIZE", "256")))
DAILY_PATH = os.getenv("QDRANT_REBUILD_DAILY_PATH", "/data/gdelt/gdelt_events")
DAILY_START = os.getenv("QDRANT_REBUILD_DAILY_START", "20130401")
DAILY_END = os.getenv("QDRANT_REBUILD_DAILY_END", "20260604")
INTRADAY_PATH = os.getenv("QDRANT_REBUILD_INTRADAY_PATH", "/data/gdelt/gdeltv2")
INTRADAY_START = os.getenv("QDRANT_REBUILD_INTRADAY_START", "20260605")
INTRADAY_END = os.getenv("QDRANT_REBUILD_INTRADAY_END", "") or None
OFF_HOURS_ONLY = os.getenv("QDRANT_REBUILD_OFF_HOURS_ONLY", "true").lower() == "true"
OFF_HOURS_START = int(os.getenv("QDRANT_REBUILD_OFF_HOURS_START", "19"))
OFF_HOURS_END = int(os.getenv("QDRANT_REBUILD_OFF_HOURS_END", "7"))
TIMEZONE = ZoneInfo(os.getenv("QDRANT_REBUILD_TIMEZONE", "America/Chicago"))
SLEEP_SECONDS = max(10, int(os.getenv("QDRANT_REBUILD_WAIT_SECONDS", "300")))


def _default_state() -> dict:
    return {
        "phase": "events_daily",
        "sources": {
            "events_daily": {"last_completed_file": "", "current_file": "", "current_row": 0, "files_completed": 0, "processed": 0},
            "events_intraday": {"last_completed_file": "", "current_file": "", "current_row": 0, "files_completed": 0, "processed": 0},
            "articles": {"after_url": "", "processed": 0},
        },
        "complete": False,
    }


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_state()
    default = _default_state()
    default.update(state)
    default["sources"].update(state.get("sources") or {})
    for source in default["sources"].values():
        if "processed" not in source and "indexed" in source:
            source["processed"] = source.pop("indexed")
    return default


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = int(time.time())
    partial = path.with_suffix(f"{path.suffix}.tmp")
    partial.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    partial.replace(path)


def is_run_time(now: datetime | None = None) -> bool:
    if not OFF_HOURS_ONLY:
        return True
    current = now or datetime.now(TIMEZONE)
    if current.weekday() >= 5:
        return True
    if OFF_HOURS_START > OFF_HOURS_END:
        return current.hour >= OFF_HOURS_START or current.hour < OFF_HOURS_END
    return OFF_HOURS_START <= current.hour < OFF_HOURS_END


def _event_source(phase: str) -> tuple[str, str | None, str | None]:
    if phase == "events_daily":
        return DAILY_PATH, DAILY_START, DAILY_END
    return INTRADAY_PATH, INTRADAY_START, INTRADAY_END


def rebuild_event_phase(
    store: QdrantStore,
    state: dict,
    phase: str,
    *,
    max_files: int | None = None,
    state_path: Path = STATE_PATH,
) -> bool:
    source = state["sources"][phase]
    root, start_date, end_date = _event_source(phase)
    resume_file = str(source.get("current_file") or "")
    resume_row = max(0, int(source.get("current_row") or 0))
    last_completed = str(source.get("last_completed_file") or "")
    completed_this_run = 0

    for archive in iter_archives(root, start_date=start_date, end_date=end_date):
        if last_completed and archive.name <= last_completed:
            continue
        if resume_file and archive.name < resume_file:
            continue
        row_offset = resume_row if archive.name == resume_file else 0
        batch = []
        last_row = row_offset
        for row_number, row in enumerate(iter_archive_rows(archive), start=1):
            if row_number <= row_offset:
                continue
            last_row = row_number
            item = normalize_event_row(row)
            if item:
                batch.append(item)
            if len(batch) >= BATCH_SIZE:
                written = store.upsert_items(
                    GDELT_EVENT_COLLECTION,
                    batch,
                    update_mode="insert_only",
                )
                source["processed"] = int(source.get("processed") or 0) + written
                source["current_file"] = archive.name
                source["current_row"] = row_number
                save_state(state, state_path)
                batch.clear()
        if batch:
            written = store.upsert_items(
                GDELT_EVENT_COLLECTION,
                batch,
                update_mode="insert_only",
            )
            source["processed"] = int(source.get("processed") or 0) + written
        source["files_completed"] = int(source.get("files_completed") or 0) + 1
        source["last_completed_file"] = archive.name
        source["current_file"] = ""
        source["current_row"] = 0
        save_state(state, state_path)
        logger.info(
            "%s rebuilt through %s (%s files, %s selected records)",
            phase,
            archive.name,
            source["files_completed"],
            source["processed"],
        )
        completed_this_run += 1
        resume_row = 0
        if max_files is not None and completed_this_run >= max_files:
            return False

    source["complete"] = True
    source["current_file"] = ""
    source["current_row"] = 0
    save_state(state, state_path)
    return True


def rebuild_articles(
    store: QdrantStore,
    state: dict,
    *,
    max_pages: int | None = None,
    state_path: Path = STATE_PATH,
) -> bool:
    source = state["sources"]["articles"]
    pages = 0
    while True:
        rows = list_qdrant_rebuild_articles(source.get("after_url") or "", BATCH_SIZE)
        if not rows:
            source["complete"] = True
            save_state(state, state_path)
            return True
        items = [
            {
                "id": row["url"],
                "url": row["url"],
                "title": row.get("title") or "",
                "summary": row.get("summary") or row.get("title") or "",
                "published_ts": row.get("published_ts") or 0,
                "source": row.get("source") or "unknown",
                "provider": row.get("provider") or "",
                "tickers": row.get("tickers") or [],
            }
            for row in rows
        ]
        written = store.upsert_items(NEWS_COLLECTION, items, update_mode="insert_only")
        source["processed"] = int(source.get("processed") or 0) + written
        source["after_url"] = rows[-1]["url"]
        pages += 1
        save_state(state, state_path)
        logger.info("Publisher articles rebuilt through %s (%s processed)", source["after_url"], source["processed"])
        if max_pages is not None and pages >= max_pages:
            return False


def rebuild_once(
    *,
    store: QdrantStore | None = None,
    state_path: Path = STATE_PATH,
    max_files: int | None = None,
    max_article_pages: int | None = None,
) -> dict:
    state = load_state(state_path)
    if state.get("complete"):
        return state
    store = store or QdrantStore()
    store.ensure_runtime_collections()
    while not state.get("complete"):
        phase = state.get("phase") or "events_daily"
        if phase in {"events_daily", "events_intraday"}:
            finished = rebuild_event_phase(
                store,
                state,
                phase,
                max_files=max_files,
                state_path=state_path,
            )
            if not finished:
                return state
            state["phase"] = "events_intraday" if phase == "events_daily" else "articles"
            save_state(state, state_path)
            continue
        if phase == "articles":
            finished = rebuild_articles(
                store,
                state,
                max_pages=max_article_pages,
                state_path=state_path,
            )
            if not finished:
                return state
            state["phase"] = "complete"
            state["complete"] = True
            save_state(state, state_path)
            break
        raise ValueError(f"Unknown Qdrant rebuild phase: {phase}")
    return state


def run_forever() -> dict:
    store = QdrantStore()
    store.ensure_runtime_collections()
    while True:
        state = load_state()
        if state.get("complete"):
            state["status"] = "complete"
            save_state(state)
            return state
        if is_run_time():
            state["status"] = "running"
            state.pop("error", None)
            save_state(state)
            try:
                # Bound each pass so the worker rechecks the off-hours gate
                # between archive files and article pages.
                rebuild_once(store=store, max_files=1, max_article_pages=1)
            except Exception as exc:
                state = load_state()
                state["status"] = "error"
                state["error"] = str(exc)[:1000]
                save_state(state)
                raise
            continue
        state = load_state()
        state["status"] = "waiting_for_off_hours"
        save_state(state)
        logger.info("Qdrant historical rebuild is waiting for off-hours")
        time.sleep(SLEEP_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Rebuild Qdrant from raw GDELT and Neo4j sources")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-article-pages", type=int)
    args = parser.parse_args()
    if args.status:
        result = load_state()
    elif args.max_files is not None or args.max_article_pages is not None:
        result = rebuild_once(max_files=args.max_files, max_article_pages=args.max_article_pages)
    else:
        result = run_forever()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
