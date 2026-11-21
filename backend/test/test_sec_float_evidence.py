from copy import deepcopy

import pytest

from drl_agent.sec_float_evidence import build_evidence
from drl_agent.sec_filing_float import extract_float
from drl_agent.sec_share_history import build_history
from drl_agent.historical_watch import build_watch
from test.test_sec_filing_float import filing
from test.test_sec_share_history import archive
from test.test_historical_watch import inputs, CONFIG
from test.test_synthetic_execution import resign


def disclosure():
    return extract_float(build_history(archive()), filing())


def test_preserves_units_provenance_and_measurement_age_expiry():
    source = disclosure()
    before = deepcopy(source)
    result = build_evidence([source], maximum_age_days=550)
    row, = result["evidence"]
    assert row["basis"] == "sec_reported_nonaffiliate_proxy"
    assert row["value"] == 100000
    assert row["available_ts"] == source["records"][0]["available_ts"]
    assert row["valid_until_ts"] == row["effective_ts"] + 550 * 86400
    assert row["source_float_sha256"] == source["float_sha256"]
    assert source == before
    assert not build_evidence([source], maximum_age_days=1)["evidence"]


@pytest.mark.parametrize("advance", [True, False])
def test_actions_cut_off_at_first_knowable_effective_time(advance):
    source = disclosure()
    available = source["records"][0]["available_ts"]
    event = dict(symbol="TEST", kind="split", effective_ts=available + 120,
                 available_ts=available + (60 if advance else 180), source="fixture")
    result = build_evidence([source], maximum_age_days=550, invalidations=[event])
    assert result["evidence"][0]["valid_until_ts"] == available + (120 if advance else 180)
    event.update(effective_ts=available - 120, available_ts=available - 60)
    assert not build_evidence([source], maximum_age_days=550, invalidations=[event])["evidence"]


def test_bad_policy_tampering_and_ambiguous_disclosures_fail():
    source = disclosure()
    for age in (True, 0, 731, 1.5):
        with pytest.raises(ValueError):
            build_evidence([source], maximum_age_days=age)
    modified = deepcopy(source)
    modified["records"][0]["value"] += 1
    with pytest.raises(ValueError):
        build_evidence([modified], maximum_age_days=550)
    resign(modified, "float_sha256")
    with pytest.raises(ValueError, match="duplicate"):
        build_evidence([source, modified], maximum_age_days=550)
    with pytest.raises(ValueError, match="corporate-action"):
        build_evidence([source], maximum_age_days=550, invalidations=[{}])


def test_proxy_opt_in_and_expiry_do_not_fall_back_to_older_float():
    datasets, features, evidence = inputs("A")
    ts = datasets[0]["states"][0]["decision_ts"]
    proxy = {**evidence[1], "basis": "sec_reported_nonaffiliate_proxy",
             "effective_ts": ts + 60, "available_ts": ts + 120, "valid_until_ts": ts + 240}
    evidence.append(proxy)
    strict = build_watch(datasets, features, evidence, config=CONFIG)
    assert strict["frames"][1]["watching"]
    assert strict["frames"][2]["entry_blocked"]["A"] == ["sec_float_proxy_not_enabled"]
    opted = build_watch(datasets, features, evidence, config=CONFIG, allow_sec_float_proxy=True)
    candidate = opted["frames"][2]["watching"][0]
    assert candidate["float_basis"] == "sec_reported_nonaffiliate_proxy"
    assert candidate["float_age_seconds"] == 60
    assert "float_valid_until_ts" not in candidate
    assert opted["frames"][4]["entry_blocked"]["A"] == ["historical_float_unknown"]
    assert opted["research_policy"]["allow_sec_float_proxy"]


def test_future_invalidation_does_not_change_earlier_values_or_admission():
    datasets, features, evidence = inputs("A")
    ts = datasets[0]["states"][0]["decision_ts"]
    evidence[1]["basis"] = "sec_reported_nonaffiliate_proxy"
    before = build_watch(datasets, features, evidence, config=CONFIG, allow_sec_float_proxy=True)
    evidence[1]["valid_until_ts"] = ts + 240
    after = build_watch(datasets, features, evidence, config=CONFIG, allow_sec_float_proxy=True)
    # Audit hashes change with the validity policy, but no future event date or
    # event details are exposed in the candidate's numerical/categorical values.
    for result in (before, after):
        for frame in result["frames"][:4]:
            for candidate in frame["watching"]:
                candidate.pop("evidence_ids")
    assert before["frames"][:4] == after["frames"][:4]
    assert not after["frames"][4]["watching"][0]["research_entry_ready"]
