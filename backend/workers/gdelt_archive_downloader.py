import argparse
import concurrent.futures
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


logger = logging.getLogger(__name__)
# GDELT's HTTPS certificate does not currently match data.gdeltproject.org.
# Match the provider's published archive URLs and reject invalid ZIP payloads.
BASE_URL = "http://data.gdeltproject.org/gdeltv2"
LAST_UPDATE_URL = f"{BASE_URL}/lastupdate.txt"
TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"


def latest_export_timestamp(text: str) -> str:
    for line in text.splitlines():
        match = re.search(r"/(\d{14})\.export\.CSV\.zip(?:\s|$)", line)
        if match:
            return match.group(1)
    return ""


def iter_timestamps(start: str, end: str):
    current = datetime.strptime(start, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    final = datetime.strptime(end, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    while current <= final:
        yield current.strftime(TIMESTAMP_FORMAT)
        current += timedelta(minutes=15)


def _valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return bool(archive.namelist()) and archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


def download_archive(output: Path, timestamp: str, kind: str, *, timeout: float = 90) -> str:
    filename = f"{timestamp}.{kind}.CSV.zip"
    destination = output / filename
    if destination.exists() and _valid_zip(destination):
        return "existing"
    partial = destination.with_suffix(f"{destination.suffix}.part")
    url = f"{BASE_URL}/{filename}"
    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                if response.status_code == 404:
                    return "missing"
                response.raise_for_status()
                with partial.open("wb") as target:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            target.write(chunk)
            if not _valid_zip(partial):
                raise ValueError("downloaded file is not a valid ZIP archive")
            partial.replace(destination)
            return "downloaded"
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    return "failed"


def download_range(output: str | Path, start: str, end: str, *, workers: int = 4) -> dict:
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    jobs = [
        (timestamp, kind)
        for timestamp in iter_timestamps(start, end)
        for kind in ("export", "mentions")
    ]
    stats = {"start": start, "end": end, "total": len(jobs), "downloaded": 0, "existing": 0, "missing": 0, "failed": 0}
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(download_archive, output_path, timestamp, kind): (timestamp, kind)
            for timestamp, kind in jobs
        }
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            timestamp, kind = futures[future]
            try:
                result = future.result()
                stats[result] += 1
            except Exception as exc:
                stats["failed"] += 1
                failures.append(f"{timestamp}.{kind}: {exc}")
            if completed % 50 == 0 or completed == len(jobs):
                logger.info("GDELT catch-up download %s/%s: %s", completed, len(jobs), stats)
    stats["failures"] = failures[:20]
    (output_path / "catchup_download_state.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Download a resumable range of official GDELT v2 Event and Mentions archives")
    parser.add_argument("output")
    parser.add_argument("--start", required=True, help="First 15-minute timestamp in YYYYMMDDHHMMSS format")
    parser.add_argument("--end", help="Last timestamp; defaults to GDELT's latest published export")
    parser.add_argument("--workers", type=int, default=int(os.getenv("GDELT_DOWNLOAD_WORKERS", "4")))
    args = parser.parse_args()
    logging.basicConfig(level="INFO")
    end = args.end
    if not end:
        response = requests.get(LAST_UPDATE_URL, timeout=30)
        response.raise_for_status()
        end = latest_export_timestamp(response.text)
    if not end:
        raise RuntimeError("GDELT latest export timestamp was unavailable")
    stats = download_range(args.output, args.start, end, workers=args.workers)
    print(json.dumps(stats, indent=2))
    if stats["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
