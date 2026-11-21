import hashlib
import json
from functools import partial

import pytest

from drl_agent.alpaca_data_cache import cache_bars
from drl_agent.dataset import encode_dataset
from drl_agent.historical_price_acquisition import acquire
from test.test_alpaca_data_cache import BAR, Response


def plan():
    rows = []
    for symbol in ("ABC", "DEF"):
        start, end = 1531747800, 1531747920
        identity = hashlib.sha256(encode_dataset([symbol, start, end, "sip", "raw"]).encode()).hexdigest()
        rows.append(dict(symbol=symbol, start_ts=start, end_ts=end, request_id=identity))
    result = dict(version="historical-corpus-price-plan-v1", requests=rows)
    result["plan_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def run(tmp_path, get, **kwargs):
    return acquire(plan(), tmp_path / "cache", tmp_path / "run", key="key", secret="secret",
                   fetch=partial(cache_bars, get=get), pause=lambda _: None, **kwargs)


def test_bounded_resume_retains_archives(tmp_path):
    calls = []
    def get(url, **kwargs):
        symbol = kwargs["params"]["symbols"]
        calls.append(symbol)
        return Response({"bars": {symbol: [BAR]}})
    first = run(tmp_path, get, max_requests=1)
    assert first["status"] == "paused_at_batch_limit" and first["remaining"] == 1
    second = run(tmp_path, get)
    assert second["status"] == "complete" and calls == ["ABC", "DEF"]
    run(tmp_path, lambda *a, **kw: pytest.fail("Unexpected network request"))
    assert "secret" not in (tmp_path / "run/progress.json").read_text()


def test_empty_is_reported_not_retried(tmp_path):
    result = run(tmp_path, lambda *a, **kw: Response({"bars": {}}))
    assert all(row["status"] == "no_data" for row in result["completed"].values())
    run(tmp_path, lambda *a, **kw: pytest.fail("Unexpected network request"))


def test_provider_failure_stops_and_resume_retries(tmp_path):
    with pytest.raises(RuntimeError, match="stopped"):
        run(tmp_path, lambda *a, **kw: Response({}, 429))
    report = json.loads((tmp_path / "run/progress.json").read_text())
    assert report["status"] == "failed" and not report["completed"]
    assert report["http_status"] == 429
    assert run(tmp_path, lambda *a, **kw: Response({"bars": {}}))["status"] == "complete"


def test_corrupt_completed_cache_fails_before_network(tmp_path):
    run(tmp_path, lambda *a, **kw: Response({"bars": {"ABC": [BAR]}}), max_requests=1)
    path = next((tmp_path / "cache").glob("*.json"))
    archive = json.loads(path.read_text())
    archive["summary"]["rows"] = 500
    path.write_text(json.dumps(archive))
    with pytest.raises(ValueError, match="digest"):
        run(tmp_path, lambda *a, **kw: pytest.fail("Unexpected network request"))


def test_tampered_plan_fails(tmp_path):
    value = plan()
    value["requests"][0]["end_ts"] += 60
    with pytest.raises(ValueError, match="digest"):
        acquire(value, tmp_path / "cache", tmp_path / "run")
