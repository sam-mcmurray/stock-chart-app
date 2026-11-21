import csv
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

import workers.gdelt_event_importer as gdelt_event_importer
from workers.gdelt_event_importer import (
    _headline_from_url,
    build_selected_event_id_index,
    import_events,
    normalize_event_row,
)


def _row(size=58, *, event_id="123", url="https://example.com/stock-market-rises"):
    row = [""] * size
    row[0] = event_id
    row[1] = "20240719"
    row[6] = "FEDERAL RESERVE"
    row[16] = "INVESTORS"
    row[26] = "010"
    row[28] = "01"
    row[29] = "1"
    row[30] = "1.5"
    row[31] = "4"
    row[32] = "2"
    row[33] = "3"
    row[34] = "-2.25"
    if size >= 61:
        row[52] = "New York, United States"
        row[59] = "20240719123000"
        row[60] = url
    else:
        row[50] = "New York, United States"
        row[51] = "US"
        row[56] = "20240719"
        row[57] = url
    return row


def _write_archive(path: Path, rows):
    csv_path = path.parent / path.name.removesuffix(".zip")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, delimiter="\t", lineterminator="\n").writerows(rows)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(csv_path, arcname=csv_path.name)
    csv_path.unlink()


def test_normalize_event_row_supports_58_and_61_columns():
    daily = normalize_event_row(_row(58))
    realtime = normalize_event_row(_row(61, event_id="456"))

    assert daily["id"].startswith("gdelt:20240719:")
    assert daily["metadata"]["location"] == "New York, United States"
    assert daily["metadata"]["country_code"] == "US"
    assert realtime["id"].startswith("gdelt:20240719:")
    assert realtime["id"] == daily["id"]
    assert realtime["published_ts"] > daily["published_ts"]


def test_headline_is_inferred_from_descriptive_url_slug():
    url = "https://example.com/news/Everest-Group-Announces-Executive-Appointments-47417150.html"

    assert _headline_from_url(url) == "Everest Group Announces Executive Appointments"
    assert normalize_event_row(_row(58, url=url))["title"] == "Everest Group Announces Executive Appointments"


def test_normalize_event_row_filters_non_financial_events():
    row = _row(58, url="https://example.com/local-festival-opens")
    row[6] = "CITY COUNCIL"
    row[16] = "LOCAL RESIDENTS"

    assert normalize_event_row(row) is None
    assert normalize_event_row(row, include_all=True) is not None


def test_import_events_dry_run_reports_samples(tmp_path):
    archive = tmp_path / "20240719.export.CSV.zip"
    unrelated = _row(58, event_id="456", url="https://example.com/community-picnic")
    unrelated[6] = "CITY COUNCIL"
    unrelated[16] = "LOCAL RESIDENTS"
    _write_archive(archive, [_row(58), unrelated])

    stats = import_events(archive, dry_run=True)

    assert stats["rows_seen"] == 2
    assert stats["selected"] == 1
    assert stats["skipped"] == 1
    assert stats["imported"] == 0
    assert stats["samples"][0]["id"].startswith("gdelt:20240719:")


def test_import_events_checkpoints_completed_archives(tmp_path):
    archive = tmp_path / "20240719.export.CSV.zip"
    state_path = tmp_path / "state.json"
    _write_archive(archive, [_row(58)])
    indexer = MagicMock()

    first = import_events(archive, indexer=indexer, state_path=state_path, batch_size=1)
    second = import_events(archive, indexer=indexer, state_path=state_path, batch_size=1)

    assert first["imported"] == 1
    assert second["archives_seen"] == 0
    assert indexer.upsert_batch.call_count == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["completed_files"] == [archive.name]


def test_import_events_deduplicates_source_urls_within_archive(tmp_path):
    archive = tmp_path / "20240719.export.CSV.zip"
    _write_archive(archive, [_row(58), _row(58, event_id="456")])

    stats = import_events(archive, dry_run=True)

    assert stats["matched_rows"] == 2
    assert stats["selected"] == 1
    assert stats["duplicate_urls"] == 1


def test_build_selected_event_id_index_is_checkpointed(tmp_path):
    archive = tmp_path / "20240719.export.CSV.zip"
    _write_archive(archive, [
        _row(58, event_id="123", url="https://example.com/stock-market-rises"),
        _row(58, event_id="456", url="https://example.com/bank-earnings-rise"),
    ])
    index_path = tmp_path / "event_ids.txt"
    state_path = tmp_path / "event_ids_state.json"

    event_ids, first = build_selected_event_id_index(
        tmp_path,
        start_date="20240719",
        end_date="20240719",
        index_path=index_path,
        state_path=state_path,
    )
    repeated_ids, second = build_selected_event_id_index(
        tmp_path,
        start_date="20240719",
        end_date="20240719",
        index_path=index_path,
        state_path=state_path,
    )

    assert event_ids == {"123", "456"}
    assert repeated_ids == event_ids
    assert first["new_event_ids"] == 2
    assert second["archives_seen"] == 0
    assert second["archives_skipped"] == 1
    assert set(index_path.read_text(encoding="utf-8").splitlines()) == event_ids


def test_import_events_does_not_open_chroma_when_archives_are_checkpointed(tmp_path, monkeypatch):
    archive = tmp_path / "20240719.export.CSV.zip"
    _write_archive(archive, [_row(58)])
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "completed_files": [archive.name],
        "current_file": None,
        "current_row": 0,
    }), encoding="utf-8")
    indexer_factory = MagicMock()
    monkeypatch.setattr(gdelt_event_importer, "NewsIndexer", indexer_factory)

    stats = import_events(tmp_path, state_path=state_path)

    assert stats["archives_seen"] == 0
    indexer_factory.assert_not_called()
