import json
from unittest.mock import MagicMock

from services import research_snapshots as snapshots


class MemoryRedis:
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key)
    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values: return False
        self.values[key] = value
        return True
    def delete(self, key): self.values.pop(key, None)
    def eval(self, script, count, *args):
        key, token = args[0], args[count]
        if self.get(key) != token: return 0
        if count == 2:
            self.set(args[1], args[3])
            self.delete(args[1] + ":error")
            return 1
        if 'DEL' in script: self.delete(key)
        return 1


def test_single_build_for_all_report_keys_and_cached_result(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(snapshots, "r", redis)
    thread = MagicMock()
    monkeypatch.setattr(snapshots.threading, "Thread", thread)
    builder = MagicMock(return_value={"usable_outcome_count": 7})
    assert snapshots.request("report", {"horizon": 60}, builder)[1]
    assert snapshots.request("report", {"horizon": 60}, builder)[1]
    assert snapshots.request("prices", {}, builder)[1]
    assert thread.call_count == 1
    builder.assert_not_called()
    key, token, job = thread.call_args.kwargs["args"]
    snapshots._build(key, token, job)
    result, pending = snapshots.request("report", {"horizon": 60}, builder)
    assert not pending
    assert result["usable_outcome_count"] == 7
    assert result["snapshot"]["stale"] is False
    assert snapshots.LEASE_KEY not in redis.values


def test_stale_results_remain_labeled_and_failures_back_off(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(snapshots, "r", redis)
    thread = MagicMock()
    monkeypatch.setattr(snapshots.threading, "Thread", thread)
    builder = MagicMock(side_effect=RuntimeError("secret SQL"))
    snapshots.request("report", {}, builder)
    key, token, job = thread.call_args.kwargs["args"]
    snapshots._build(key, token, job)
    assert redis.get(key + ":error") == "RuntimeError"
    assert snapshots.request("report", {}, builder) == ({"status": "retrying", "retry_after_seconds": 5}, True)
    redis.set(key, json.dumps({"generated_ts": 1, "result": {"count": 3}}))
    result, pending = snapshots.request("report", {}, builder)
    assert not pending
    assert result["snapshot"] == {"generated_ts": 1, "stale": True, "refresh_failed": True}


def test_old_worker_cannot_release_new_workers_lease(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(snapshots, "r", redis)
    monkeypatch.setattr(snapshots.threading, "Thread", MagicMock())
    redis.set(snapshots.LEASE_KEY, "new")
    snapshots._build("key", "old", lambda: {})
    assert redis.get(snapshots.LEASE_KEY) == "new"
    assert redis.get("key") is None
