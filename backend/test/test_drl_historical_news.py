from copy import deepcopy

import pytest

from drl_agent.historical_news import build_universe

START = 1500000000
OPTIONS = dict(start_ts=START, end_ts=START + 86400, enrichment_cutoff_ts=1800000000)


def article(**changes):
    return {"record_type": "news", "record_id": "old", "version_key": "v1",
            "published_ts": START + 100, "observed_ts": 1790000000,
            "pipeline_version": "frozen-model-v2", "article_url": "https://example.com/old",
            "result": {"relevance_label": "finance", "article_verification_status": "publisher",
                       "entity_link_version": 2, "related_tickers": ["ABC", "XYZ", "ABC"]}, **changes}


def test_old_article_admitted_without_rewriting_actual_observation_time():
    rows = [article()]
    before = deepcopy(rows)
    report = build_universe(rows, **OPTIONS)
    assert rows == before
    candidate = report["candidates"][0]
    assert candidate["replay_available_ts"] == START + 160
    assert candidate["enriched_observed_ts"] == 1790000000
    assert candidate["tickers"] == ["ABC", "XYZ"]
    assert report["retrospective"] and not report["training_ready"]
    assert not report["live_activation_eligible"]
    assert report == build_universe(rows, **OPTIONS)


def test_latest_correction_wins_before_relevance_filter():
    newer = article(version_key="v2", observed_ts=1790000001)
    newer["result"]["relevance_label"] = "politics"
    report = build_universe([article(), newer], **OPTIONS)
    assert report["selected_article_count"] == 0
    assert report["rejections"] == {"unrelated": 1}


def test_cutoff_excludes_later_enrichment_and_order_is_stable():
    newer = article(version_key="v2", observed_ts=1800000001)
    rows = [article(), newer]
    report = build_universe(rows, **OPTIONS)
    assert report["candidates"][0]["enrichment_version_key"] == "v1"
    assert report == build_universe(list(reversed(rows)), **OPTIONS)


def test_price_requests_merge_overlap_without_selecting_future_winners():
    report = build_universe([article(), article(record_id="other", version_key="v2")], **OPTIONS)
    assert report["selected_article_count"] == 2
    assert len(report["price_requests"]) == 2 * 37
    for symbol in ("ABC", "XYZ"):
        requests = [r for r in report["price_requests"] if r["symbol"] == symbol]
        assert requests[0]["start_ts"] == START + 160 - 30 * 86400
        assert requests[-1]["end_ts"] == START + 160 + 7 * 86400
        assert all(r["end_ts"] - r["start_ts"] <= 86400 for r in requests)
        assert all(a["end_ts"] == b["start_ts"] for a, b in zip(requests, requests[1:]))
        assert all(r["provider"] is None and r["status"] == "coverage_required" for r in requests)


@pytest.mark.parametrize("field,value,reason", [
    ("article_verification_status", "unverified", "unverified_article"),
    ("entity_link_version", 1, "unverified_company_links"),
    ("related_tickers", "ABC", "invalid_tickers"),
    ("related_tickers", [], "no_company_tickers")])
def test_unverified_links_not_promoted(field, value, reason):
    row = article()
    row["result"][field] = value
    report = build_universe([row], **OPTIONS)
    assert report["rejections"] == {reason: 1}
    assert not report["price_requests"]


def test_revised_publication_outside_window_excluded():
    report = build_universe([article(), article(version_key="v2", observed_ts=1790000001,
                                              published_ts=START + 86400)], **OPTIONS)
    assert report["rejections"] == {"publication_outside_window": 1}


def test_bounds_fail_closed():
    with pytest.raises(ValueError):
        build_universe([], **{**OPTIONS, "end_ts": START + 32 * 86400})
    with pytest.raises(ValueError):
        build_universe([], **OPTIONS, processing_delay_seconds=-1)
