import pytest

from drl_agent.historical_candidate_probe import probe
from test.test_synthetic_execution import resign


def manifest():
    source = {"version": "retrospective-news-universe-v1",
              "price_requests": [{"symbol": s} for s in ("B", "A", "B", "C")]}
    resign(source, "manifest_sha256")
    return source


def test_bounded_deterministic_universe_price_and_availability(tmp_path):
    calls = []
    def cache(symbol, start, end, output, **kwargs):
        calls.append((symbol, start, end))
        return dict(first_close=10 if symbol == "A" else 30, archive_sha256="fixture",
                    file="/archive/test.json", reused=True)
    result = probe(manifest(), "2026-07-17T13:30:00Z", tmp_path, minimum_price=1, maximum_price=20,
                   limit=2, cache=cache, sleep=lambda _: pytest.fail("unexpected sleep"))
    assert [c[0] for c in calls] == ["A", "B"]
    assert result["next_offset"] == 2 and result["universe_count"] == 3
    assert [r["status"] for r in result["rows"]] == ["price_in_range", "price_out_of_range"]
    assert result["rows"][0]["available_ts"] == 1784295060
    assert not result["training_ready"]


def test_no_bar_recorded_but_provider_errors_not_swallowed(tmp_path):
    def missing(*args, **kwargs):
        raise ValueError("No bars returned; no cache published")
    result = probe(manifest(), "2026-07-17T13:30:00Z", tmp_path, minimum_price=1, maximum_price=20,
                   offset=2, limit=2, cache=missing, sleep=lambda _: None)
    assert result["next_offset"] == 3
    assert result["rows"] == [{"symbol": "C", "status": "no_bar_at_probe_time"}]
    def failure(*args, **kwargs):
        raise RuntimeError("HTTP 429")
    with pytest.raises(RuntimeError, match="429"):
        probe(manifest(), "2026-07-17T13:30:00Z", tmp_path, minimum_price=1, maximum_price=20, cache=failure)


def test_integrity_and_request_bounds(tmp_path):
    for extra in ({"limit": 101}, {"offset": -1}, {"maximum_price": 0}):
        with pytest.raises(ValueError):
            probe(manifest(), "2026-07-17T13:30:00Z", tmp_path,
                  **{**dict(minimum_price=1, maximum_price=20), **extra})
    source = manifest()
    source["price_requests"].append({"symbol": "D"})
    with pytest.raises(ValueError):
        probe(source, "2026-07-17T13:30:00Z", tmp_path, minimum_price=1, maximum_price=20)
