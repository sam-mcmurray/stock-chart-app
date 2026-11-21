import json
from unittest.mock import Mock

import pytest

from services import financial_news_gate as gate
from services.news_rejection import receipt


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(gate.relevance, "_runtime_config", lambda *a: ("ollama", "http://local", "small"))
    response = Mock()
    post = Mock(return_value=response)
    monkeypatch.setattr(gate.relevance.requests, "post", post)
    full = Mock(return_value={})
    monkeypatch.setattr(gate.relevance, "classify_articles", full)

    def respond(rows):
        response.json.return_value = {"message": {"content": json.dumps({"items": rows})}}
    return respond, post, full


def decision(i, label="unrelated", confidence=.99):
    return {"record_index": i, "decision": label, "confidence": confidence, "reason": "Routine local crime"}


def test_unrelated_is_discarded_without_enrichment_translation_or_review(setup):
    respond, post, full = setup
    respond([decision(0)])
    result = gate.classify_articles([{"id": "crime", "title": "Police investigate local murder after neighborhood dispute"}])
    full.assert_not_called()
    assert post.call_count == 1
    assert gate.relevance.should_remove(result["crime"])
    assert receipt(result["crime"])["rejected_receipt"]
    assert "display_headline" not in result["crime"]
    schema = post.call_args.kwargs["json"]["format"]["properties"]["items"]
    assert schema["minItems"] == schema["maxItems"] == 1
    assert schema["items"]["properties"]["record_index"]["maximum"] == 0


def test_schema_is_bounded_per_batch_without_mutating_shared_template(setup):
    respond, post, _ = setup
    respond([decision(0, "keep"), decision(1, "keep")])
    gate.screen([{"id": "a"}, {"id": "b"}])
    schema = post.call_args.kwargs["json"]["format"]["properties"]["items"]
    assert schema["minItems"] == schema["maxItems"] == 2
    assert schema["items"]["properties"]["record_index"]["maximum"] == 1
    assert "maxItems" not in gate.SCHEMA["properties"]["items"]


def test_empty_screen_does_not_call_model(setup):
    _, post, _ = setup
    assert gate.screen([]) == {}
    post.assert_not_called()


@pytest.mark.parametrize("label,confidence", [("keep", .99), ("uncertain", .99), ("unrelated", .5)])
def test_financial_or_ambiguous_news_is_enriched(setup, label, confidence):
    respond, _, full = setup
    respond([decision(0, label, confidence)])
    item = {"id": "company", "title": "Company executive killed; board announces interim chief"}
    gate.classify_articles([item])
    full.assert_called_once_with([item], allow_disabled=False)


def test_inferred_headline_alone_cannot_be_deleted(setup):
    respond, _, full = setup
    respond([decision(0)])
    item = {"id": "unknown", "title": "url slug about local murder", "title_inferred": True}
    gate.classify_articles([item])
    full.assert_called_once()


@pytest.mark.parametrize("rows", [[], [decision(0), decision(0)], [decision(10)], [decision(0, confidence=float('nan'))]])
def test_invalid_gate_results_never_delete_or_run_full_enrichment(setup, rows):
    respond, _, full = setup
    respond(rows)
    with pytest.raises(ValueError):
        gate.classify_articles([{"id": "a", "title": "Police investigate routine local murder"}])
    full.assert_not_called()


def test_rejections_survive_failure_in_relevant_sibling(setup):
    respond, _, full = setup
    respond([decision(0), decision(1, "keep")])
    full.side_effect = RuntimeError("model unavailable")
    with pytest.raises(gate.relevance.PartialClassificationError) as error:
        gate.classify_articles([{"id": "crime", "title": "Police investigate routine local murder"},
                                {"id": "finance", "title": "Company announces quarterly earnings"}])
    assert set(error.value.results) == {"crime"}
    assert error.value.failed_ids == {"finance"}
