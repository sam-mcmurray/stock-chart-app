from copy import deepcopy
import json

import pytest

from drl_agent.alpaca_actions import URL, cache_actions, split_invalidations
from drl_agent.sec_float_evidence import build_evidence
from test.test_sec_float_evidence import disclosure
from test.test_synthetic_execution import resign


def split(**kwargs):
    return dict(id="split-1", symbol="TEST", old_rate=1, new_rate=4, ex_date="2024-09-01", **kwargs)


class Response:
    status_code = 200
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, size):
        yield json.dumps(self.payload).encode()


def source(tmp_path):
    saved, _ = cache_actions("TEST", "2024-08-01", "2024-09-02", tmp_path, key="key", secret="secret",
        get=lambda *a, **kw: Response({"corporate_actions": {"forward_splits": [split()]}}))
    return saved


def test_cache_pages_reuse_and_no_credentials(tmp_path):
    calls = []
    def get(url, **kw):
        assert url == URL and not kw["allow_redirects"]
        calls.append(kw["params"])
        return Response({"corporate_actions": {"forward_splits": [split()]},
                         "next_page_token": "page2" if len(calls) == 1 else None})
    saved, reused = cache_actions("TEST", "2024-08-01", "2024-09-02", tmp_path,
                                 key="credential-key", secret="credential-secret", get=get)
    assert not reused and len(calls) == 2 and calls[1]["page_token"] == "page2"
    again, reused = cache_actions("TEST", "2024-08-01", "2024-09-02", tmp_path,
                                 get=lambda *a, **kw: pytest.fail("network called"))
    assert reused and again == saved
    assert "credential-" not in next(tmp_path.glob("*.json")).read_text()
    assert len(split_invalidations(saved)["invalidations"]) == 1


def test_default_does_not_backdate_availability_and_proxy_is_explicit(tmp_path):
    saved = source(tmp_path)
    strict = split_invalidations(saved)
    assumed = split_invalidations(saved, allow_effective_date_proxy=True)
    a, b = strict["invalidations"][0], assumed["invalidations"][0]
    assert a["available_ts"] > a["effective_ts"]
    assert b["available_ts"] == b["effective_ts"]
    assert a["availability_basis"] == "actual_archive_collection"
    assert b["retrospective"] and not strict["coverage_complete"]
    evidence = build_evidence([disclosure()], maximum_age_days=550, invalidations=assumed)
    assert evidence["evidence"][0]["valid_until_ts"] == b["effective_ts"]
    assert evidence["source_invalidations_sha256"] == assumed["invalidations_sha256"]


def test_incomplete_unrelated_and_unsupported_events_are_not_silently_accepted(tmp_path):
    saved = source(tmp_path)
    groups = saved["responses"][0]["corporate_actions"]
    groups["forward_splits"] += [{**split(), "id": "missing-date", "ex_date": None},
                                  {**split(), "id": "other", "symbol": "OTHER"},
                                  {**split(), "id": "bad-ratio", "new_rate": 0}]
    groups["cash_dividends"] = [{"id": "cash"}]
    resign(saved, "archive_sha256")
    result = split_invalidations(saved)
    assert len(result["invalidations"]) == 1 and len(result["excluded"]) == 4


def test_conflicts_and_tampering_refused(tmp_path):
    saved = source(tmp_path)
    bad = deepcopy(saved)
    bad["responses"][0]["corporate_actions"]["forward_splits"].append({**split(), "new_rate": 2})
    with pytest.raises(ValueError):
        split_invalidations(bad)
    resign(bad, "archive_sha256")
    with pytest.raises(ValueError, match="Conflicting"):
        split_invalidations(bad)
    with pytest.raises(ValueError, match="boolean"):
        split_invalidations(saved, allow_effective_date_proxy="yes")


def test_bad_response_loop_and_http_failure_publish_no_archive(tmp_path):
    def loop(*a, **kw):
        return Response({"corporate_actions": {}, "next_page_token": "same"})
    with pytest.raises(ValueError, match="pagination"):
        cache_actions("TEST", "2024-08-01", "2024-09-02", tmp_path, key="k", secret="s", get=loop)
    assert not list(tmp_path.iterdir())
    response = Response({})
    response.status_code = 403
    with pytest.raises(RuntimeError, match="403"):
        cache_actions("TEST", "2024-08-01", "2024-09-02", tmp_path, key="k", secret="s",
                      get=lambda *a, **kw: response)
    assert not list(tmp_path.iterdir())


def test_empty_snapshot_is_retained_without_claiming_coverage(tmp_path):
    saved, _ = cache_actions("TEST", "2024-08-01", "2024-09-02", tmp_path, key="k", secret="s",
                            get=lambda *a, **kw: Response({"corporate_actions": {}}))
    assert saved["rows"] == 0 and not saved["coverage_complete"]
    assert not split_invalidations(saved)["invalidations"]
