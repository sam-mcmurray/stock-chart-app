from types import SimpleNamespace

import pytest

from workers.news_llm_evaluator import cases, check_result, evaluate


def result_for(case):
    expected = case["expected"]
    return {
        "relevance_label": "business" if expected["relevant"] else "unrelated",
        "related_tickers": expected["tickers"],
        "display_headline": "English translation of the headline" if expected["translated"] else case["article"]["title"],
        "translated_to_english": expected["translated"],
        "llm_summary": "A factual summary.",
        "company_relationships": [{"relationship": "partnership", "validated_tickers_a": ["NVDA"],
                                   "validated_tickers_b": ["MSFT"]}] if expected["relationship"] else [],
    }


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["article"]["id"])
def test_expected_results_pass(case):
    assert check_result(case, result_for(case)) == []


def test_false_ticker_and_uncertainty_are_not_passes():
    case = cases()[0]
    result = result_for(case)
    result.update(related_tickers=["WEST", "BANK"], relevance_label="uncertain")
    assert check_result(case, result) == ["relevance", "tickers"]
    assert check_result(case, {}) == ["missing_result"]


def test_translation_flag_alone_is_insufficient():
    case = cases()[-1]
    result = result_for(case)
    result["display_headline"] = case["article"]["title"]
    assert check_result(case, result) == ["untranslated_headline"]


def test_evaluation_instruments_requests_and_continues_after_failure():
    response = SimpleNamespace(status_code=200, ok=True, json=lambda: {"eval_count": 20})
    original = SimpleNamespace(post=lambda *args, **kwargs: response)
    classifier = SimpleNamespace(requests=original, RELEVANCE_POLICY_VERSION="test", _translation_errors=lambda result: [])
    by_id = {case["article"]["id"]: case for case in cases()}

    def classify(items):
        item = items[0]
        classifier.requests.post("test", json={"messages": [{"content": "input"}]})
        if item["id"] == "geography":
            raise ValueError("test failure")
        classifier.requests.post("test", json={"messages": [{"content": "input\nCORRECTION: verify"}]})
        return {item["id"]: result_for(by_id[item["id"]])}

    classifier.classify_articles = classify
    report = evaluate(classifier)
    assert classifier.requests is original
    assert report["passed"] == 7
    assert report["total"] == 8
    assert report["requests"] == 15
    assert report["verification_requests"] == 7
    assert report["rows"][0]["errors"] == ["ValueError: test failure"]
    assert report["rows"][1]["calls"][0]["eval_count"] == 20
