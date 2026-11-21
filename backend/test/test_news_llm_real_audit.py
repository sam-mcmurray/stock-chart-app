import json
import sys

import pytest

from workers.news_llm_real_audit import collect_inputs, consistency_checks, select_candidates


def test_sample_is_bounded_diverse_and_partitioned():
    recent = [{"url": f"https://recent{i}.example/a", "published_ts": 1790000000} for i in range(20)]
    history = [{"url": f"https://old{i}.example/a", "published_ts": 1500000000} for i in range(20)]
    history += [{"url": f"https://new{i}.example/a", "published_ts": 1700000000} for i in range(20)]
    rows = select_candidates(recent, history)
    assert len(rows) == 16
    assert [r["group"] for r in rows].count("recent") == 8
    assert [r["group"] for r in rows].count("historical_older") == 4
    assert [r["group"] for r in rows].count("historical_newer") == 4


def test_duplicate_hosts_urls_and_invalid_schemes_are_excluded():
    rows = [{"url": "https://example.com/a"}, {"url": "https://www.example.com/b"},
            {"url": "https://example.com/a"}, {"url": "file:///etc/passwd"}, {"url": "https://[broken"}]
    assert len(select_candidates(rows, [])) == 1


def test_audit_uses_publisher_content_not_previous_ai_summary():
    candidates = [{"url": "https://example.com/a", "title": "Company announces new cloud service",
                   "summary": "Previous AI output must not become source evidence", "group": "recent"}]
    samples, skipped = collect_inputs(candidates, lambda _: {
        "title": candidates[0]["title"], "summary": "Publisher summary", "article_text": "X" * 4000})
    assert not skipped
    assert samples[0]["expected"] is None
    assert samples[0]["article"]["summary"] == "Publisher summary"
    assert len(samples[0]["article"]["article_text"]) == 2500


def test_missing_pages_and_redirected_homepages_are_skipped():
    candidates = [{"url": "https://example.com/a", "title": "Company announces new cloud service", "group": "recent"}]
    assert collect_inputs(candidates, lambda _: {})[1][0]["reason"] == "publisher_content_unavailable"
    samples, skipped = collect_inputs(candidates, lambda _: {"title": "Welcome to the publisher", "summary": "Read our news"})
    assert not samples
    assert skipped[0]["reason"] == "publisher_identity_not_confirmed"


def test_consistency_checks_catch_unsupported_evidence_and_tickers():
    case = {"article": {"title": "Company reports revenue", "summary": "Revenue increased ten percent."}}
    result = {"display_headline": "Company&#039;s revenue", "llm_summary": "Revenue increased.",
              "related_tickers": ["ABC"], "related_companies": [{"evidence": "invented quotation"}]}
    assert consistency_checks(case, result) == ["company_evidence_not_in_input", "html_entities_display_headline", "tickers_without_provenance"]
    assert consistency_checks(case, {}) == ["missing_result"]


def test_grounded_output_passes_consistency_not_semantic_accuracy():
    case = {"article": {"title": "Company reports revenue", "summary": "Revenue increased ten percent."}}
    result = {"display_headline": case["article"]["title"], "llm_summary": case["article"]["summary"],
              "related_tickers": ["ABC"], "related_companies": [{"evidence": "Revenue increased ten percent", "validated_tickers": ["ABC"]}]}
    assert consistency_checks(case, result) == []


def test_saved_input_replay_never_queries_article_storage_or_fetches_pages(monkeypatch, tmp_path):
    import redis
    from services import article_metadata
    from workers import news_llm_real_audit as audit

    saved = tmp_path / "saved.json"
    snapshot = {"samples": [{"article": {"id": "a"}, "expected": None}], "skipped": [], "captured_at": 123}
    saved.write_text(json.dumps(snapshot))
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["audit", "--inputs", str(saved), "--output", str(output)])
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: object())
    monkeypatch.setattr(audit, "snapshots", lambda _: {})

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must use the saved inputs")

    monkeypatch.setattr(audit, "load_candidates", forbidden)
    monkeypatch.setattr(article_metadata, "fetch_article_metadata", forbidden)

    def evaluate(_classifier, samples, _checker):
        assert samples == snapshot["samples"]
        return {"total": 1, "passed": 1, "requests": 1, "verification_requests": 0,
                "request_wall_seconds": 1, "verification_wall_seconds": 0, "rows": []}

    monkeypatch.setattr(audit, "evaluate", evaluate)
    with pytest.raises(SystemExit) as exit_info:
        audit.main()
    assert exit_info.value.code == 0
    assert json.loads(output.read_text())["semantic_accuracy"] is None
    assert json.loads(output.with_suffix(".inputs.json").read_text()) == snapshot
