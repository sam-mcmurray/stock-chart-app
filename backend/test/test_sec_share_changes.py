from copy import deepcopy

import pytest

from drl_agent.sec_share_changes import build_changes
from drl_agent.sec_share_history import build_history
from drl_agent.sec_float_evidence import build_evidence
from test.test_sec_share_history import archive, fact
from test.test_sec_float_evidence import disclosure
from test.test_synthetic_execution import resign


def history(*extra):
    source = archive()
    source["payload"]["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"].extend(extra)
    resign(source, "archive_sha256")
    return build_history(source)


def test_change_is_not_confirmed_dilution_and_preserves_filing_availability():
    h = history(fact(1200, end="2024-09-30", filed="2024-11-01", accn="0000001800-24-000002"))
    before = deepcopy(h)
    result = build_changes(h, "TEST")
    event, = result["invalidations"]
    assert event["direction"] == "increase" and event["change_shares"] == 200
    assert event["change_fraction"] == .2
    assert not event["confirmed_dilution"] and event["cause"] == "unknown"
    assert event["available_ts"] > event["effective_ts"]
    evidence = build_evidence([disclosure()], maximum_age_days=550, invalidations=result)
    assert evidence["evidence"][0]["valid_until_ts"] == event["available_ts"]
    assert h == before


def test_unchanged_count_not_an_event_and_decreases_also_invalidate():
    h = history(fact(1000, end="2024-08-31", filed="2024-09-01"),
                fact(900, end="2024-09-30", filed="2024-11-01"))
    events = build_changes(h, "TEST")["invalidations"]
    assert len(events) == 1 and events[0]["direction"] == "decrease"
    assert events[0]["interval_start_date"] == "2024-08-31"


def test_late_old_measurement_does_not_replace_newer_baseline():
    h = history(fact(1200, end="2024-09-30", filed="2024-11-01"),
                fact(800, end="2024-07-31", filed="2024-12-01"),
                fact(1300, end="2024-12-31", filed="2025-02-01"))
    result = build_changes(h, "TEST")
    assert [e["change_shares"] for e in result["invalidations"]] == [200, 100]
    assert result["excluded"][0]["reason"] == "older_measurement_arrived_later"


def test_conflicts_reset_baseline_and_revisions_are_labeled():
    h = history(fact(1100, filed="2024-09-01"),
                fact(1200, end="2024-09-30", filed="2024-11-01", accn="0000001800-24-000002"),
                fact(1300, end="2024-09-30", filed="2024-11-01", accn="0000001800-24-000003"),
                fact(1400, end="2024-12-31", filed="2025-02-01"))
    result = build_changes(h, "TEST")
    assert len(result["invalidations"]) == 2
    assert result["invalidations"][0]["change_basis"] == "same_date_disclosure_revision"
    assert result["invalidations"][1]["change_basis"] == "conflicting_disclosures_uncertainty"
    assert result["excluded"][0]["reason"] == "conflicting_latest_counts"


def test_identity_and_tampering():
    h = history(fact(1200, end="2024-09-30", filed="2024-11-01"))
    result = build_changes(h, "TEST")
    result["invalidations"][0]["cik"] = 999
    resign(result, "invalidations_sha256")
    evidence = build_evidence([disclosure()], maximum_age_days=550, invalidations=result)
    row = evidence["evidence"][0]
    assert row["valid_until_ts"] == row["effective_ts"] + 550 * 86400
    h["records"][0]["value"] = 555
    with pytest.raises(ValueError):
        build_changes(h, "TEST")
