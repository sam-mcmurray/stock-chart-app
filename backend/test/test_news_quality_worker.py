from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from workers.news_quality_worker import build_issue, issue_key, next_audit_at


def test_news_quality_issue_key_is_stable_per_type_and_url():
    assert issue_key("missing_title", "https://example.test/a") == issue_key(
        "missing_title", "https://example.test/a"
    )
    assert issue_key("missing_title", "https://example.test/a") != issue_key(
        "invalid_url", "https://example.test/a"
    )


def test_build_issue_keeps_reviewable_context():
    issue = build_issue(
        "ticker_overload",
        "medium",
        {"url": "https://example.test/a", "title": "Example"},
        {"tickers": ["A", "B", "C", "D", "E"]},
    )

    assert issue["title"] == "Example"
    assert issue["article_url"] == "https://example.test/a"
    assert issue["details"]["tickers"][-1] == "E"


def test_next_audit_at_schedules_off_hours_without_startup_run():
    zone = ZoneInfo("America/Chicago")

    assert next_audit_at(datetime(2026, 7, 19, 1, 0, tzinfo=zone)) == datetime(
        2026, 7, 19, 2, 30, tzinfo=zone
    )
    assert next_audit_at(datetime(2026, 7, 19, 10, 0, tzinfo=zone)) == datetime(
        2026, 7, 20, 2, 30, tzinfo=zone
    )


def test_next_audit_at_rejects_invalid_time():
    with pytest.raises(ValueError, match="HH:MM"):
        next_audit_at(audit_time="25:00")
