import json
from unittest.mock import MagicMock

import redis

from workers import live_research_outcome_worker as worker


def test_subscribed_symbols_requires_fresh_connected_stream(monkeypatch):
    payload = {
        "status": "connected",
        "ts": 1_000,
        "symbols": ["mara", "PATH", "MARA", ""],
    }
    monkeypatch.setattr(worker.r, "get", MagicMock(return_value=json.dumps(payload)))

    assert worker._subscribed_symbols(now_ts=1_020) == ["MARA", "PATH"]
    assert worker._subscribed_symbols(now_ts=1_031) == []


def test_subscribed_symbols_fails_closed_on_invalid_heartbeat(monkeypatch):
    monkeypatch.setattr(worker.r, "get", MagicMock(return_value="not-json"))

    assert worker._subscribed_symbols(now_ts=1_000) == []


def test_heartbeat_timeout_does_not_interrupt_outcome_collection(monkeypatch):
    publish = MagicMock(side_effect=redis.RedisError("timed out"))
    monkeypatch.setattr(worker.r, "set", publish)

    worker._publish_heartbeat({"status": "ok", "ts": 1_000})

    publish.assert_called_once()
