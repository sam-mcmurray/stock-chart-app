import json
from unittest.mock import MagicMock

import pytest

from services import scanner_float as service


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setenv("SCANNER_FLOAT_MAX_CACHE_SECONDS", "86400")
    client = MagicMock()
    client.get.return_value = None
    client.incr.return_value = 1
    return client


def test_primary_persists_source_and_retrieval_time(cache):
    result = service.resolve_float("TEST", 100, 200, cache, now=1000)
    assert result["float_shares"] == 100
    assert result["float_as_of"] is None
    assert result["float_status"] == "reported"
    record = json.loads(cache.set.call_args.args[1])
    assert record["observed_ts"] == 1000
    cache.get.return_value = json.dumps(record)
    result = service.resolve_float("TEST", None, 200, cache, now=1100)
    assert result["float_shares"] == 100
    assert result["float_status"] == "cached"
    assert result["float_observed_ts"] == 1000
    assert cache.set.call_count == 1  # Cache reuse never extends the evidence age.


@pytest.mark.parametrize("now,outstanding,symbol", [(90000, 200, "TEST"), (900, 200, "TEST"),
    (1100, 300, "TEST"), (1100, 200, "WRONG")])
def test_expired_future_changed_supply_or_wrong_identity_not_reused(cache, now, outstanding, symbol):
    cache.get.return_value = json.dumps({"symbol": symbol, "value": 100, "source": "yfinance.floatShares",
        "observed_ts": 1000, "shares_outstanding": 200})
    result = service.resolve_float("TEST", None, outstanding, cache, now=now)
    assert result["float_shares"] is None


@pytest.mark.parametrize("value", [None, -1, 0, float("nan"), float("inf"), True, 1.2, 201])
def test_bad_primary_does_not_become_float(cache, value):
    result = service.resolve_float("TEST", value, 200, cache, now=1000)
    assert result["float_shares"] is None
    assert result["float_fallback_reason"] == "no_usable_cached_float"


def test_cache_failure_preserves_primary_and_leaves_missing_float_unknown(cache):
    cache.get.side_effect = RuntimeError("Redis unavailable")
    cache.set.side_effect = RuntimeError("Redis unavailable")
    assert service.resolve_float("TEST", 100, 200, cache, now=1000)["float_shares"] == 100
    result = service.resolve_float("TEST", None, 200, cache, now=1000)
    assert result["float_shares"] is None
    assert result["float_fallback_reason"] == "no_usable_cached_float"
