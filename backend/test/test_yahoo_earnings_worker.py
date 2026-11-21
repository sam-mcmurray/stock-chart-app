from datetime import date
from unittest.mock import MagicMock

from workers import yahoo_earnings_worker


def test_sync_day_pages_and_checkpoints(monkeypatch):
    client = MagicMock()
    stored = []
    calendar = []
    calls = []

    def load(day, *, offset, size):
        calls.append((day.isoformat(), offset, size))
        if offset == 0:
            return ([{
                "id": "yahoo:first",
                "source": "yahoo",
                "symbol": "AAA",
                "event_date": "2025-11-06",
            }], 150)
        return ([{
            "id": "yahoo:second",
            "source": "yahoo",
            "symbol": "BBB",
            "event_date": "2025-11-06",
        }], 150)

    monkeypatch.setattr(yahoo_earnings_worker, "r", client)
    monkeypatch.setattr(yahoo_earnings_worker, "load_earnings_page", load)
    monkeypatch.setattr(
        yahoo_earnings_worker.persistent_store,
        "upsert_earnings_events",
        lambda rows: stored.extend(rows),
    )
    monkeypatch.setattr(
        yahoo_earnings_worker.persistent_store,
        "upsert_calendar_events",
        lambda rows: calendar.extend(rows),
    )
    monkeypatch.setattr(
        yahoo_earnings_worker,
        "calendar_event",
        lambda row: {"id": f"earnings:{row['id']}"},
    )

    result = yahoo_earnings_worker.sync_day(
        date(2025, 11, 6),
        request_gate=lambda: None,
    )

    assert calls == [
        ("2025-11-06", 0, 100),
        ("2025-11-06", 100, 100),
    ]
    assert [row["symbol"] for row in stored] == ["AAA", "BBB"]
    assert len(calendar) == 2
    assert result["complete"] is True
    assert result["next_offset"] == 200
    client.set.assert_any_call(yahoo_earnings_worker.K_OFFSET, 200)


def test_market_hours_pause_does_not_apply_on_weekends(monkeypatch):
    monkeypatch.setattr(yahoo_earnings_worker, "OFF_HOURS_ONLY", True)

    assert yahoo_earnings_worker._off_hours(
        yahoo_earnings_worker.datetime(2026, 7, 25, 12, tzinfo=yahoo_earnings_worker.TIMEZONE)
    ) is True
    assert yahoo_earnings_worker._off_hours(
        yahoo_earnings_worker.datetime(2026, 7, 24, 12, tzinfo=yahoo_earnings_worker.TIMEZONE)
    ) is False


def test_backfill_skips_weekend_calendar_dates_without_request(monkeypatch):
    client = MagicMock()
    client.get.side_effect = lambda key: {
        yahoo_earnings_worker.K_CURSOR: "2026-07-19",
        yahoo_earnings_worker.K_OFFSET: "100",
    }.get(key)
    load = MagicMock()
    monkeypatch.setattr(yahoo_earnings_worker, "r", client)
    monkeypatch.setattr(yahoo_earnings_worker, "load_earnings_page", load)
    monkeypatch.setattr(yahoo_earnings_worker, "START_DATE", "2000-01-01")

    result = yahoo_earnings_worker.backfill_once()

    assert result["skipped"] == "weekend"
    assert result["complete"] is True
    load.assert_not_called()
    client.set.assert_any_call(yahoo_earnings_worker.K_CURSOR, "2026-07-18")
    client.delete.assert_called_once_with(yahoo_earnings_worker.K_OFFSET)


def test_progress_tolerates_redis_outage(monkeypatch):
    client = MagicMock()
    client.get.side_effect = RuntimeError("redis unavailable")
    monkeypatch.setattr(yahoo_earnings_worker, "r", client)

    assert yahoo_earnings_worker._progress() == {"cursor": None, "offset": 0}


def test_heartbeat_tolerates_redis_outage(monkeypatch):
    client = MagicMock()
    client.set.side_effect = RuntimeError("redis unavailable")
    monkeypatch.setattr(yahoo_earnings_worker, "r", client)

    yahoo_earnings_worker._heartbeat("error", error="database unavailable")
