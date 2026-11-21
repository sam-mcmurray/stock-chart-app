import threading
import time
from unittest.mock import Mock

from services.scanner_company_cache import CompanyCache


def test_slow_loader_is_nonblocking_deduplicated_and_bounded():
    entered, release = threading.Event(), threading.Event()

    def loader(symbol):
        entered.set()
        assert release.wait(5)
        return {"name": symbol}

    cache = CompanyCache(loader, ttl=100, capacity=1)
    try:
        assert cache.get("A") == ({}, "pending")
        assert entered.wait(2)
        assert cache.get("A") == ({}, "pending")
        assert cache.get("B") == ({}, "pending")
        assert cache.get("C") == ({}, "deferred")
        assert cache.status()["pending"] == 2
        assert cache.status()["active_symbol"] == "A"
    finally:
        release.set()
    cache.queue.join()
    assert cache.get("A") == ({"name": "A"}, "fresh")
    assert cache.status()["completed"] == 2
    info, _ = cache.get("A")
    info["name"] = "changed"
    assert cache.get("A")[0]["name"] == "A"


def test_stale_value_returned_during_refresh_and_failure_recovers():
    entered, release = threading.Event(), threading.Event()

    def loader(symbol):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("offline")

    cache = CompanyCache(loader, ttl=100)
    cache.records["A"] = (time.time() - 200, {"name": "old"})
    try:
        assert cache.get("A") == ({"name": "old"}, "pending")
        assert entered.wait(2)
    finally:
        release.set()
    cache.queue.join()
    assert cache.status()["failed"] == 1
    assert cache.status()["pending"] == 0
    cache.loader = lambda _: {"name": "new"}
    cache.get("A")
    cache.queue.join()
    assert cache.get("A") == ({"name": "new"}, "fresh")


def test_scanner_clears_expired_float_without_provider_calls(monkeypatch):
    from workers import scanner_worker as worker

    cache = Mock()
    cache.get.return_value = ({"name": "Company", "float_shares": 100,
                              "float_observed_ts": 1, "float_source": "yfinance.floatShares"}, "pending")
    monkeypatch.setattr(worker, "_COMPANY_CACHE", cache)
    monkeypatch.setattr(worker.yf, "Ticker", Mock(side_effect=AssertionError("inline lookup")))
    info = worker._company_info("test")
    assert info["name"] == "Company"
    assert info["float_shares"] is None
    assert info["float_status"] == "unknown"
    cache.get.assert_called_once_with("TEST")


def test_scanner_preserves_float_observation_while_refreshing(monkeypatch):
    from workers import scanner_worker as worker

    observed = time.time() - 60
    cache = Mock()
    cache.get.return_value = ({"float_shares": 100, "float_observed_ts": observed,
                              "float_source": "yfinance.floatShares"}, "pending")
    monkeypatch.setattr(worker, "_COMPANY_CACHE", cache)
    info = worker._company_info("TEST")
    assert info["float_shares"] == 100
    assert info["float_observed_ts"] == observed
    assert info["float_status"] == "cached"


def test_scanner_cold_cache_is_unknown(monkeypatch):
    from workers import scanner_worker as worker

    cache = Mock()
    cache.get.return_value = ({}, "deferred")
    monkeypatch.setattr(worker, "_COMPANY_CACHE", cache)
    info = worker._company_info("TEST")
    assert info["float_shares"] is None
    assert info["shares_outstanding"] is None
    assert info["metadata_refresh_status"] == "deferred"


def test_expired_float_requests_refresh_even_with_fresh_metadata(monkeypatch):
    from workers import scanner_worker as worker

    cache = Mock()
    cache.get.side_effect = [({"float_shares": 100, "float_observed_ts": 1}, "fresh"),
                             ({}, "pending")]
    monkeypatch.setattr(worker, "_COMPANY_CACHE", cache)
    info = worker._company_info("TEST")
    assert info["float_shares"] is None
    assert info["metadata_refresh_status"] == "pending"
    cache.get.assert_called_with("TEST", force_refresh=True)
