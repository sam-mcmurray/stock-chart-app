import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from services import financial_article_analysis as analysis


def test_analysis_only_runs_for_long_candidates(monkeypatch):
    monkeypatch.setattr(analysis, "ENABLED", True)
    assert analysis.should_analyze_candidate({"action": "long_candidate"}) is True
    assert analysis.should_analyze_candidate({"action": "watch"}) is False


def test_negative_analysis_can_only_downgrade_candidate():
    decision = {"action": "long_candidate", "reasons": []}
    result = analysis.apply_analysis_guardrail(decision, {
        "source": "llm",
        "sentiment": "negative",
        "catalyst_direction": "negative",
        "blocking_risks": ["offering"],
    })
    assert result["action"] == "avoid"
    assert "llm_negative_financial_analysis" in result["reasons"]


def test_positive_analysis_does_not_promote_watch():
    decision = {"action": "watch", "reasons": []}
    result = analysis.apply_analysis_guardrail(decision, {
        "source": "llm",
        "sentiment": "positive",
        "catalyst_direction": "positive",
        "blocking_risks": [],
    })
    assert result["action"] == "watch"
