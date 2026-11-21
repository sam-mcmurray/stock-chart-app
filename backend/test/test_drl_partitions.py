from copy import deepcopy
import hashlib

import pytest

from drl_agent.dataset import encode_dataset
from drl_agent.partitions import partition_sessions
from test.test_drl_portfolio_competition import shift
from test.test_drl_portfolio_replay import observations, WATCH


def sign(data):
    data["manifest"].pop("dataset_sha256", None)
    data["manifest"]["dataset_sha256"] = hashlib.sha256(encode_dataset(data).encode()).hexdigest()
    return data


def day(offset, symbol="A", evidence=None, **kwargs):
    data = shift(observations(symbol, **kwargs), offset * 86400)
    for state in data["states"]:
        for kind, items in state["inputs"].items():
            for item in items:
                item["id"] += f"-{offset}"
                key = "id" if kind == "scanner" else "observation_key"
                item["record"][key] = item["id"]
                if kind == "scanner":
                    item["record"]["payload"]["semantic_signal"]["hype"]["evidence_article_ids"] = [evidence or f"{symbol}-{offset}-news"]
    return sign(data)


def run(sessions, **kwargs):
    return partition_sessions(sessions, watch_config=WATCH, train_end="2026-09-22",
                              validation_end="2026-09-23", embargo_seconds=0, **kwargs)


def test_chronological_whole_session_splits_and_fillable_coverage():
    sources = [[day(i, "A"), day(i, "B")] for i in range(3)]
    before = deepcopy(sources)
    report = run(sources)
    assert [report["partitions"][s][0]["date"] for s in ("train", "validation", "test")] == [
        "2026-09-22", "2026-09-23", "2026-09-24"]
    assert all(c["fillable_entry_probes"] > 0 for c in report["coverage"].values())
    assert report["basic_coverage_passed"] and not report["training_ready"]
    assert not report["purged"]
    assert report == run(sources)
    assert sources == before


def test_shared_news_across_companies_purges_earlier_whole_session():
    result = run([[day(0, "A", evidence="shared"), day(0, "B")], [day(1)], [day(2, "C", evidence="shared")]])
    assert not result["partitions"]["train"]
    assert result["purged"][0]["symbols"] == ["A", "B"]
    assert "shared_news_or_market_evidence" in result["purged"][0]["reasons"]
    assert "train:empty_after_purge" in result["coverage_blockers"]


def test_embargo_accounts_for_input_history_not_just_decision_grid():
    result = partition_sessions([[day(i)] for i in range(3)], watch_config=WATCH,
                                train_end="2026-09-22", validation_end="2026-09-23", embargo_seconds=86400)
    assert len(result["purged"]) == 2
    assert all("observation_interval_or_embargo_overlap" in r["reasons"] for r in result["purged"])


def test_missing_depth_and_watch_inputs_are_coverage_failures_not_removed_cases():
    old = day(0)
    for state in old["states"]:
        for item in state["inputs"]["scanner"]:
            item["record"]["payload"].pop("watch_inputs")
    report = run([[sign(old)], [day(1, book=False)], [day(2)]])
    assert len(report["partitions"]["train"]) == 1
    assert report["partitions"]["train"][0]["watch_rejections"]["watch_inputs_missing"] > 0
    assert report["coverage_blockers"] == ["train:no_fillable_watch_candidates", "validation:no_fillable_watch_candidates"]


def test_forged_digest_or_future_observations_rejected():
    bad = day(0)
    bad["states"][0]["symbol"] = "FORGED"
    with pytest.raises(ValueError, match="digest"):
        run([[bad]])
    bad = day(0)
    bad["states"][0]["inputs"]["scanner"][0]["record"]["payload"]["semantic_signal"]["hype"]["as_of_ts"] += 20
    with pytest.raises(ValueError, match="unavailable"):
        run([[sign(bad)]])


def test_duplicate_dates_reversed_dates_and_boundaries_rejected():
    for sessions in ([[day(0)], [day(0)]], [[day(1)], [day(0)]]):
        with pytest.raises(ValueError, match="chronological"):
            run(sessions)
    with pytest.raises(ValueError, match="boundary"):
        partition_sessions([[day(0)]], watch_config=WATCH, train_end="2026-09-23", validation_end="2026-09-22")


def test_shared_market_record_ids_are_purged_even_with_different_news():
    early, late = day(0), day(2)
    for state in late["states"]:
        for item in state["inputs"]["levelone_quote"]:
            item["id"] = item["id"].replace("-2", "-0")
            item["record"]["observation_key"] = item["id"]
    result = run([[early], [day(1)], [sign(late)]])
    assert "shared_news_or_market_evidence" in result["purged"][0]["reasons"]
