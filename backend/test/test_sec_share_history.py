from copy import deepcopy
from datetime import datetime
import json

import pytest

from drl_agent.sec_share_history import build_history, cache_companyfacts, facts_as_of
from test.test_synthetic_execution import resign


def fact(value=1000, end="2024-06-30", filed="2024-08-01", accn="0000001800-24-000001"):
    return dict(val=value, end=end, filed=filed, accn=accn, form="10-Q")


def archive():
    result = {"version": "sec-companyfacts-cache-v1", "payload": {"cik": 1800, "entityName": "Fixture",
        "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [fact()]}},
                           "EntityPublicFloat": {"units": {"USD": [fact(1_000_000)]}}}}}}
    resign(result, "archive_sha256")
    return result


def ts(value):
    return int(datetime.fromisoformat(value).timestamp())


def test_reported_units_and_filing_time_no_lookahead():
    source = archive()
    before = deepcopy(source)
    history = build_history(source)
    assert history["counts"] == {"shares_outstanding": 1, "public_float_value": 1}
    assert all(r["float_share_count"] is None for r in history["records"])
    assert not facts_as_of(history, ts("2024-08-02T03:59:59+00:00"))
    current = facts_as_of(history, ts("2024-08-02T04:00:00+00:00"))
    assert current["public_float_value"]["records"][0]["unit"] == "USD"
    assert current["shares_outstanding"]["records"][0]["value"] == 1000
    assert source == before
    assert not history["training_ready"]


def test_revisions_only_visible_after_their_own_filing_and_conflicts_retained():
    source = archive()
    rows = source["payload"]["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
    rows.extend([fact(1200, filed="2024-09-01", accn="0000001800-24-000002"),
                 fact(1300, filed="2024-09-01", accn="0000001800-24-000003")])
    resign(source, "archive_sha256")
    history = build_history(source)
    assert facts_as_of(history, ts("2024-08-15T00:00:00+00:00"))["shares_outstanding"]["records"][0]["value"] == 1000
    later = facts_as_of(history, ts("2024-09-03T00:00:00+00:00"))["shares_outstanding"]
    assert later["conflicting"] and len(later["records"]) == 2


def test_unsupported_units_bad_dates_and_duplicate_facts_are_reported():
    source = archive()
    units = source["payload"]["facts"]["dei"]["EntityPublicFloat"]["units"]
    units["shares"] = [fact()]
    units["USD"].extend([fact(1_000_000), fact(-1), fact(filed="2024-01-01")])
    resign(source, "archive_sha256")
    history = build_history(source)
    assert history["excluded"] == {"unsupported_unit": 1, "duplicate_fact": 1, "invalid_fact": 2}


class Response:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield json.dumps(archive()["payload"]).encode()


def test_cache_reuse_without_contact_and_no_contact_in_saved_data(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000001800.json"
        assert not kwargs["allow_redirects"]
        return Response()
    cached, reused = cache_companyfacts(1800, tmp_path, user_agent="UnitTest tests@unit.test", get=get)
    assert not reused
    again, reused = cache_companyfacts(1800, tmp_path, get=lambda *a, **kw: pytest.fail("Network called"))
    assert reused and again == cached and len(calls) == 1
    assert "tests@unit.test" not in next(tmp_path.glob("*.json")).read_text()
    assert build_history(cached)["counts"]["shares_outstanding"] == 1


def test_refuses_fake_contact_backdated_download_and_tampered_cache(tmp_path):
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        cache_companyfacts(1800, tmp_path, user_agent="App admin@example.com")
    with pytest.raises(ValueError, match="older snapshot"):
        cache_companyfacts(1800, tmp_path, snapshot="2000-01-01")
    assert not list(tmp_path.iterdir())
    cache_companyfacts(1800, tmp_path, user_agent="UnitTest tests@unit.test", get=lambda *a, **kw: Response())
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text())
    payload["payload"]["entityName"] = "Changed"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="digest"):
        cache_companyfacts(1800, tmp_path, get=lambda *a, **kw: pytest.fail("Network called"))
