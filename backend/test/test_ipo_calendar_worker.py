from unittest.mock import MagicMock

from workers import ipo_calendar_worker


def test_heartbeat_tolerates_redis_outage(monkeypatch):
    client = MagicMock()
    client.set.side_effect = RuntimeError("redis unavailable")
    monkeypatch.setattr(ipo_calendar_worker, "r", client)

    ipo_calendar_worker._heartbeat("error", error="database unavailable")
