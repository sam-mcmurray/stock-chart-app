import csv
import io
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from workers.gdelt_event_importer import normalize_event_row

logger = logging.getLogger(__name__)

GDELT_LAST_UPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_EXPORT_PATTERN = re.compile(r"(?P<timestamp>\d{14})\.export\.CSV\.zip$")
GDELT_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"


def _latest_export_url(text: str) -> str:
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[-1].endswith(".export.CSV.zip"):
            return parts[-1]
    return ""


def get_latest_gdelt_export_url(timeout: float = 30) -> str:
    listing = requests.get(GDELT_LAST_UPDATE_URL, timeout=timeout)
    listing.raise_for_status()
    return _latest_export_url(listing.text)


def pending_export_urls(previous_url: str, latest_url: str, max_batches: int) -> list[str]:
    if not latest_url or max_batches <= 0:
        return []
    latest_match = GDELT_EXPORT_PATTERN.search(latest_url)
    previous_match = GDELT_EXPORT_PATTERN.search(previous_url or "")
    if not latest_match or not previous_match:
        return [latest_url]

    latest = datetime.strptime(latest_match.group("timestamp"), GDELT_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    previous = datetime.strptime(previous_match.group("timestamp"), GDELT_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    if previous >= latest:
        return []

    base_url = latest_url.rsplit("/", 1)[0]
    urls = []
    cursor = previous + timedelta(minutes=15)
    while cursor <= latest and len(urls) < max_batches:
        urls.append(f"{base_url}/{cursor.strftime(GDELT_TIMESTAMP_FORMAT)}.export.CSV.zip")
        cursor += timedelta(minutes=15)
    return urls


def export_gap_batches(previous_url: str, latest_url: str) -> int:
    latest_match = GDELT_EXPORT_PATTERN.search(latest_url or "")
    previous_match = GDELT_EXPORT_PATTERN.search(previous_url or "")
    if not latest_match or not previous_match:
        return 0
    latest = datetime.strptime(latest_match.group("timestamp"), GDELT_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    previous = datetime.strptime(previous_match.group("timestamp"), GDELT_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    return max(0, int((latest - previous).total_seconds() // (15 * 60)))


def _valid_zip_bytes(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return bool(archive.namelist()) and archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


def _load_zip_bytes(url: str, timeout: float, archive_dir: str | Path | None = None) -> bytes:
    configured_dir = archive_dir or os.getenv("GDELT_LIVE_ARCHIVE_PATH", "")
    destination = Path(configured_dir) / url.rsplit("/", 1)[-1] if configured_dir else None
    if destination and destination.exists():
        content = destination.read_bytes()
        if _valid_zip_bytes(content):
            return content
        logger.warning("Ignoring invalid local GDELT archive and downloading again: %s", destination)

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    content = response.content
    if not _valid_zip_bytes(content):
        raise ValueError(f"GDELT response is not a valid ZIP archive: {url}")
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(f"{destination.suffix}.part")
        partial.write_bytes(content)
        partial.replace(destination)
    return content


def load_gdelt_export_url(
    export_url: str,
    timeout: float = 30,
    archive_dir: str | Path | None = None,
) -> list[dict]:
    content = _load_zip_bytes(export_url, timeout, archive_dir)
    items = []
    seen_urls = set()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        member = next((name for name in archive.namelist() if name.lower().endswith(".csv")), None)
        if not member:
            raise ValueError(f"GDELT export contains no CSV member: {export_url}")
        with archive.open(member) as raw:
            lines = (line.decode("utf-8", errors="replace") for line in raw)
            for row in csv.reader(lines, delimiter="\t"):
                item = normalize_event_row(row)
                if not item or item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                items.append({
                    "id": item["id"],
                    "source": item["source"],
                    "headline": item["title"],
                    "summary": item["summary"],
                    "text": item["text"],
                    "gdelt_summary": item["summary"],
                    "url": item["url"],
                    "datetime": item["published_ts"],
                    "metadata": item["metadata"],
                    "event_id": item["metadata"].get("gdelt_event_id"),
                    "event_code": item["metadata"].get("event_code"),
                    "location": item["metadata"].get("location"),
                    "actor1": item["metadata"].get("actor1"),
                    "actor2": item["metadata"].get("actor2"),
                    "country_code": item["metadata"].get("country_code"),
                    "title_inferred": bool(item["metadata"].get("title_inferred_from_url")),
                })
    return items


def load_latest_gdelt_export(
    previous_url: str = "",
    timeout: float = 30,
    archive_dir: str | Path | None = None,
) -> tuple[list[dict], str]:
    try:
        export_url = get_latest_gdelt_export_url(timeout=timeout)
        if not export_url or export_url == previous_url:
            return [], export_url or previous_url
        return load_gdelt_export_url(export_url, timeout=timeout, archive_dir=archive_dir), export_url
    except Exception as exc:
        logger.warning("GDELT live export fetch failed: %s", exc)
        return [], previous_url


def load_gdelt_export_mentions(
    export_url: str,
    event_ids: set[str],
    timeout: float = 30,
    *,
    raise_on_error: bool = False,
    archive_dir: str | Path | None = None,
) -> list[dict]:
    if not export_url or not event_ids:
        return []
    mentions_url = export_url.replace(".export.CSV.zip", ".mentions.CSV.zip")
    if mentions_url == export_url:
        return []
    try:
        content = _load_zip_bytes(mentions_url, timeout, archive_dir)
        rows = []
        seen = set()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            member = next((name for name in archive.namelist() if name.lower().endswith(".csv")), None)
            if not member:
                return []
            with archive.open(member) as raw:
                lines = (line.decode("utf-8", errors="replace") for line in raw)
                for row in csv.reader(lines, delimiter="\t"):
                    if len(row) < 16 or row[0] not in event_ids:
                        continue
                    url = (row[5] or "").strip()
                    key = (row[0], url)
                    if not url.startswith(("http://", "https://")) or key in seen:
                        continue
                    seen.add(key)
                    try:
                        mention_ts = int(row[2] or 0)
                        sentence_id = int(row[6] or 0)
                        confidence = float(row[11] or 0)
                        document_tone = float(row[13] or 0)
                    except ValueError:
                        continue
                    rows.append({
                        "event_id": row[0],
                        "url": url,
                        "source": (row[4] or "").strip(),
                        "mention_ts": mention_ts,
                        "sentence_id": sentence_id,
                        "confidence": confidence,
                        "document_tone": document_tone,
                    })
        return rows
    except Exception as exc:
        logger.warning("GDELT live Mentions fetch failed: %s", exc)
        if raise_on_error:
            raise
        return []
