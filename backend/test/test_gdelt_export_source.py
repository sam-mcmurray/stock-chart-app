import io
import os
import sys
import zipfile
from unittest.mock import MagicMock, patch

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from news_sources.gdelt_export_source import (
    _latest_export_url,
    load_gdelt_export_mentions,
    load_latest_gdelt_export,
    load_gdelt_export_url,
    export_gap_batches,
    pending_export_urls,
)


def _event_row():
    row = [""] * 61
    row[0] = "1307007199"
    row[1] = "20260716"
    row[6] = "BANK"
    row[16] = "COMPANY"
    row[26] = "010"
    row[28] = "01"
    row[30] = "0"
    row[34] = "1.2"
    row[52] = "New York, United States"
    row[59] = "20260716120000"
    row[60] = "https://example.com/company-earnings-beat-market-expectations"
    return "\t".join(row).encode()


def test_latest_export_url_prefers_export_file():
    text = "1 hash http://data.gdeltproject.org/gdeltv2/20260716120000.mentions.CSV.zip\n2 hash http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"
    assert _latest_export_url(text) == "http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"


def test_pending_export_urls_returns_every_missing_quarter_hour_in_order():
    previous = "http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"
    latest = "http://data.gdeltproject.org/gdeltv2/20260716124500.export.CSV.zip"

    assert pending_export_urls(previous, latest, 16) == [
        "http://data.gdeltproject.org/gdeltv2/20260716121500.export.CSV.zip",
        "http://data.gdeltproject.org/gdeltv2/20260716123000.export.CSV.zip",
        "http://data.gdeltproject.org/gdeltv2/20260716124500.export.CSV.zip",
    ]


def test_pending_export_urls_bounds_long_outage_from_oldest_missing_batch():
    previous = "http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"
    latest = "http://data.gdeltproject.org/gdeltv2/20260716140000.export.CSV.zip"

    urls = pending_export_urls(previous, latest, 2)

    assert [url.rsplit("/", 1)[-1][:14] for url in urls] == ["20260716121500", "20260716123000"]


def test_pending_export_urls_bootstraps_with_latest_only():
    latest = "http://data.gdeltproject.org/gdeltv2/20260716124500.export.CSV.zip"

    assert pending_export_urls("", latest, 16) == [latest]


def test_export_gap_batches_reports_quarter_hour_lag():
    previous = "http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"
    latest = "http://data.gdeltproject.org/gdeltv2/20260716124500.export.CSV.zip"

    assert export_gap_batches(previous, latest) == 3


def test_export_download_is_archived_and_reused(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("events.csv", _event_row())
    response = MagicMock(content=buffer.getvalue())
    url = "http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"

    with patch("news_sources.gdelt_export_source.requests.get", return_value=response) as request:
        first = load_gdelt_export_url(url, archive_dir=tmp_path)
    with patch("news_sources.gdelt_export_source.requests.get") as request_again:
        second = load_gdelt_export_url(url, archive_dir=tmp_path)

    assert first == second
    assert (tmp_path / "20260716120000.export.CSV.zip").read_bytes() == buffer.getvalue()
    request.assert_called_once()
    request_again.assert_not_called()


def test_load_latest_export_filters_and_normalizes_finance_rows(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("events.csv", _event_row())
    listing = MagicMock(text="1 hash http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip")
    archive_response = MagicMock(content=buffer.getvalue())

    with patch("news_sources.gdelt_export_source.requests.get", side_effect=[listing, archive_response]):
        items, url = load_latest_gdelt_export(archive_dir=tmp_path)

    assert url.endswith("20260716120000.export.CSV.zip")
    assert items[0]["headline"] == "company earnings beat market expectations"
    assert items[0]["id"].startswith("gdelt:20260716:")
    assert items[0]["metadata"]["gdelt_event_id"] == "1307007199"
    assert items[0]["metadata"]["location"] == "New York, United States"


def test_load_latest_export_skips_previously_seen_file():
    url = "http://data.gdeltproject.org/gdeltv2/20260716120000.export.CSV.zip"
    listing = MagicMock(text=f"1 hash {url}")
    with patch("news_sources.gdelt_export_source.requests.get", return_value=listing) as request:
        items, returned_url = load_latest_gdelt_export(url)
    assert items == []
    assert returned_url == url
    assert request.call_count == 1


def test_load_export_mentions_filters_to_current_event_ids(tmp_path):
    row = [""] * 16
    row[0] = "1307007199"
    row[2] = "20260716121500"
    row[4] = "example.com"
    row[5] = "https://example.com/related-story"
    row[6] = "2"
    row[11] = "88"
    row[13] = "1.5"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mentions.csv", "\t".join(row))
    response = MagicMock(content=buffer.getvalue())

    with patch("news_sources.gdelt_export_source.requests.get", return_value=response):
        mentions = load_gdelt_export_mentions(
            "http://data.gdeltproject.org/gdeltv2/20260716121500.export.CSV.zip",
            {"1307007199"},
            archive_dir=tmp_path,
        )

    assert mentions == [{
        "event_id": "1307007199",
        "url": "https://example.com/related-story",
        "source": "example.com",
        "mention_ts": 20260716121500,
        "sentence_id": 2,
        "confidence": 88.0,
        "document_tone": 1.5,
    }]
