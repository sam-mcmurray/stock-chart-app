import json

import pytest

from drl_agent.alpaca_trade_gap import cache_minute, stamp_ns
from test.test_alpaca_data_cache import Response

START = "2026-06-17T14:00:00Z"
TRADE = {"t": "2026-06-17T14:00:59.999999999Z", "p": 100, "s": 3, "i": 1, "x": "V", "c": ["I"]}


def test_paginated_evidence_and_reuse(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs)
        assert url.endswith("/ABT/trades") and not kwargs["allow_redirects"]
        assert kwargs["params"]["end"] == TRADE["t"]
        trade = {**TRADE, "i": len(calls)}
        return Response({"symbol": "ABT", "trades": [trade], "next_page_token": "next" if len(calls) == 1 else None})
    result = cache_minute("ABT", START, tmp_path, key="key", secret="secret", get=get)
    assert result["trades"] == 2 and result["reported_size_sum"] == 6
    assert result["finding"] == "all_trades_include_odd_lot_condition"
    assert calls[1]["params"]["page_token"] == "next"
    assert "secret" not in (tmp_path / result["file"]).read_text()
    assert cache_minute("ABT", START, tmp_path, get=lambda *a, **k: pytest.fail("Network used"))["reused"]


def test_empty_response_is_retained_without_zero_bar(tmp_path):
    result = cache_minute("ABT", START, tmp_path, key="k", secret="s",
                          get=lambda *a, **k: Response({"symbol": "ABT", "trades": None}))
    assert result["finding"] == "no_trades_returned"
    artifact = json.loads((tmp_path / result["file"]).read_text())
    assert not artifact["training_ready"] and "candles" not in artifact


@pytest.mark.parametrize("change", [{"t": "2026-06-17T14:01:00Z"}, {"s": -1}, {"c": "I"}])
def test_bad_trade_not_published(tmp_path, change):
    with pytest.raises(ValueError):
        cache_minute("ABT", START, tmp_path, key="k", secret="s",
                     get=lambda *a, **k: Response({"symbol": "ABT", "trades": [{**TRADE, **change}]}))
    assert not list(tmp_path.glob("*.json"))


def test_corrupt_cache_fails_without_network(tmp_path):
    result = cache_minute("ABT", START, tmp_path, key="k", secret="s",
                          get=lambda *a, **k: Response({"symbol": "ABT", "trades": []}))
    path = tmp_path / result["file"]
    data = json.loads(path.read_text())
    data["summary"]["trades"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="digest"):
        cache_minute("ABT", START, tmp_path, get=lambda *a, **k: pytest.fail("Network used"))


def test_nanoseconds_not_rounded_to_following_minute():
    assert stamp_ns("2026-06-17T14:01:00Z") - stamp_ns(TRADE["t"]) == 1


@pytest.mark.parametrize("payload", [{"symbol": "ABT"}, {"symbol": "XYZ", "trades": []},
                                    {"symbol": "ABT", "trades": [TRADE, TRADE]}])
def test_malformed_or_duplicate_response_is_not_no_data(tmp_path, payload):
    with pytest.raises(ValueError):
        cache_minute("ABT", START, tmp_path, key="k", secret="s", get=lambda *a, **k: Response(payload))
    assert not list(tmp_path.glob("*.json"))


def test_repeated_page_token_cannot_publish_partial_evidence(tmp_path):
    with pytest.raises(ValueError, match="page token"):
        cache_minute("ABT", START, tmp_path, key="k", secret="s",
                     get=lambda *a, **k: Response({"symbol": "ABT", "trades": [], "next_page_token": "again"}))
    assert not list(tmp_path.glob("*.json"))
