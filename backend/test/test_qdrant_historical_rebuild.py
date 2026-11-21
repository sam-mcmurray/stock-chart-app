import csv
import json
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from workers import qdrant_historical_rebuild as rebuild


def _row(event_id: str, url: str) -> list[str]:
    row = [""] * 58
    row[0] = event_id
    row[1] = "20240719"
    row[6] = "FEDERAL RESERVE"
    row[16] = "INVESTORS"
    row[26] = "010"
    row[28] = "01"
    row[30] = "1.5"
    row[34] = "-2.25"
    row[50] = "New York, United States"
    row[51] = "US"
    row[56] = "20240719"
    row[57] = url
    return row


def _archive(path: Path, row: list[str]) -> None:
    member = path.name.removesuffix(".zip")
    content = "\t".join(row) + "\n"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)


class _Store:
    def __init__(self):
        self.items = []

    def upsert_items(self, collection, items, update_mode="upsert"):
        rows = list(items)
        self.items.extend((collection, row["id"], update_mode) for row in rows)
        return len(rows)


def test_event_rebuild_resumes_after_last_completed_archive(tmp_path, monkeypatch):
    _archive(tmp_path / "20240719.export.CSV.zip", _row("1", "https://example.com/stock-market-one"))
    _archive(tmp_path / "20240720.export.CSV.zip", _row("2", "https://example.com/stock-market-two"))
    state_path = tmp_path / "state.json"
    state = rebuild._default_state()
    store = _Store()
    monkeypatch.setattr(rebuild, "DAILY_PATH", str(tmp_path))
    monkeypatch.setattr(rebuild, "DAILY_START", "20240719")
    monkeypatch.setattr(rebuild, "DAILY_END", "20240720")

    assert rebuild.rebuild_event_phase(
        store,
        state,
        "events_daily",
        max_files=1,
        state_path=state_path,
    ) is False
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["sources"]["events_daily"]["last_completed_file"] == "20240719.export.CSV.zip"

    assert rebuild.rebuild_event_phase(
        store,
        persisted,
        "events_daily",
        state_path=state_path,
    ) is True
    assert [item[1] for item in store.items] == [
        rebuild.normalize_event_row(_row("1", "https://example.com/stock-market-one"))["id"],
        rebuild.normalize_event_row(_row("2", "https://example.com/stock-market-two"))["id"],
    ]
    assert all(item[2] == "insert_only" for item in store.items)


def test_off_hours_gate_allows_nights_and_weekends(monkeypatch):
    monkeypatch.setattr(rebuild, "OFF_HOURS_ONLY", True)
    zone = ZoneInfo("America/Chicago")

    assert rebuild.is_run_time(datetime(2026, 8, 19, 20, tzinfo=zone)) is True
    assert rebuild.is_run_time(datetime(2026, 8, 19, 12, tzinfo=zone)) is False
    assert rebuild.is_run_time(datetime(2026, 8, 22, 12, tzinfo=zone)) is True
