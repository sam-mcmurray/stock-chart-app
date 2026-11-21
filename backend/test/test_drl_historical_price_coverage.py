from datetime import datetime, timezone
import hashlib

import pytest

from drl_agent.dataset import encode_dataset
from drl_agent.historical_price_coverage import build_report, request_intervals


def manifest(day, *, offset=0):
    start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()) + offset
    data = {"version": "retrospective-news-universe-v1", "price_requests": [
        {"request_id": "r1", "symbol": "ABC", "start_ts": start, "end_ts": start + 86400,
         "frequency_minutes": 1, "provider": None}]}
    data["manifest_sha256"] = hashlib.sha256(encode_dataset(data).encode()).hexdigest()
    return data


@pytest.mark.parametrize("day,minutes", [("2026-09-22", 390), ("2026-11-27", 210),
                                        ("2026-07-03", 0), ("2026-09-20", 0)])
def test_calendar_includes_early_close_and_excludes_holiday_weekend(day, minutes):
    data = manifest(day)
    intervals = request_intervals(data)
    report = build_report(data, intervals, [])
    assert report["expected_regular_minutes"] == minutes
    assert report["present_regular_minutes"] == 0
    assert report["requests"][0]["status"] == ("missing_regular_minutes" if minutes else "no_regular_session")


def test_duplicate_counts_and_overcoverage_fail_closed():
    data = manifest("2026-09-22")
    intervals = request_intervals(data)
    for rows in ([{"request_id": "r1", "present_minutes": 391}],
                 [{"request_id": "r1", "present_minutes": 1}] * 2):
        with pytest.raises(ValueError):
            build_report(data, intervals, rows)


def test_partial_and_present_do_not_claim_training_readiness():
    data = manifest("2026-09-22")
    intervals = request_intervals(data)
    for count, status in ((200, "partial_regular_minutes"), (390, "regular_minutes_present")):
        report = build_report(data, intervals, [{"request_id": "r1", "present_minutes": count}])
        assert report["requests"][0]["status"] == status
        assert report["requests"][0]["missing_regular_minutes"] == 390 - count
        assert not report["training_ready"] and report["broker_calls"] == 0


def test_tampered_manifest_rejected():
    data = manifest("2026-09-22")
    data["price_requests"][0]["symbol"] = "WRONG"
    with pytest.raises(ValueError, match="digest"):
        request_intervals(data)


def test_nonminute_boundary_owns_each_candle_once():
    data = manifest("2026-09-22", offset=14 * 3600 + 17)
    intervals = request_intervals(data)
    assert intervals[0]["start_ts"] % 60 == 0
    assert intervals[0]["start_ts"] == data["price_requests"][0]["start_ts"] + 43
    assert sum((w["end_ts"] - w["start_ts"]) // 60 for w in intervals) == 390
