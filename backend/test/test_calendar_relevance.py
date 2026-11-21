import json
from types import SimpleNamespace

import pytest

from services import calendar_relevance


def test_generated_gdelt_summary_is_not_article_or_company_evidence(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda *a, **k: ["MSFT"])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: values)
    generated = "GDELT event 123: Microsoft recorded a cooperation event involving Unknown actor."
    article = {"id": "a", "title": "Economy and finance newspaper roundup", "summary": generated}
    payload = calendar_relevance.prepare_classification_payload([article])[0]
    assert payload["summary"] == ""
    assert article["summary"] == generated

    def post(*args, **kwargs):
        assert generated not in kwargs["json"]["messages"][0]["content"]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"message": {"content": json.dumps({"items": [{
            "record_index": 0, "label": "finance", "source_language": "English",
            "summary": generated, "companies": [{"name": "Microsoft", "ticker": "MSFT", "evidence": "Microsoft recorded"}],
        }]})}})

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    result = calendar_relevance.classify_articles([article])["a"]
    assert result["related_tickers"] == []
    assert result["related_companies"] == []
    assert result["llm_summary"] == article["title"]
    assert result["article_verification_status"] == "unverified"


def test_publisher_summary_and_article_excerpt_are_preserved():
    item = {"id": "a", "title": "Publisher headline", "summary": "Publisher summary.",
            "article_text": "Publisher article text.", "content_verified": True}
    payload = calendar_relevance.prepare_classification_payload([item])[0]
    assert payload["summary"] == item["summary"]
    assert payload["content_excerpt"] == item["article_text"]


def test_translation_repair_cannot_overwrite_analysis_or_provenance(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    original = {"source_language": "Italian", "original_headline": "economia e finanza",
                "display_headline": "economia e finanza", "llm_summary": "economia e finanza",
                "translated_to_english": True, "relevance_label": "finance", "relevance_score": 0.8,
                "related_tickers": ["TEST"], "company_relationships": [{"evidence": "source"}],
                "article_verification_status": "unverified", "relevance_reviewed": False}
    row = {"record_index": 0, "source_language": "Italian", "display_headline": "Economy and finance",
           "summary": "Economy and finance", "related_tickers": ["WRONG"], "relevance_label": "unrelated",
           "article_verification_status": "canonical"}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"message": {"content": json.dumps({"items": [row]})}}))
    result = calendar_relevance._repair_translations({"a": original}, {"a": {"title": "economia e finanza"}})["a"]
    for field in ("relevance_label", "relevance_score", "related_tickers", "company_relationships",
                  "article_verification_status", "relevance_reviewed"):
        assert result[field] == original[field]
    assert original["display_headline"] == "economia e finanza"
    assert result["summary_source"] == "llm_translation_unverified"


def test_copied_foreign_headline_is_not_a_translated_summary():
    result = {"source_language": "Italian", "translated_to_english": True,
              "original_headline": "economia e finanza dai giornali",
              "display_headline": "Economy and finance from newspapers",
              "llm_summary": "economia e finanza dai giornali"}
    assert "summary_translation_unverified" in calendar_relevance._translation_errors(result)
    result["llm_summary"] = result["display_headline"]
    assert calendar_relevance._translation_errors(result) == []
    result.update(source_language="English", translated_to_english=False,
                  original_headline=result["display_headline"])
    assert calendar_relevance._translation_errors(result) == []


def test_tariff_topics_collapse_to_trade_policy():
    assert calendar_relevance.normalize_topics(
        ["tariffs", "Trade Policy", "import duties"],
    ) == ["trade policy"]


def test_commodity_topics_are_normalized_for_existing_enrichments():
    assert calendar_relevance.normalize_commodities([], ["oil", "geopolitics"]) == ["Crude Oil"]
    assert calendar_relevance.display_topics(
        ["oil", "commodities", "geopolitics"],
        ["Crude Oil"],
    ) == ["geopolitics"]


def test_llm_relevance_classifies_companies_and_validates_tickers(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda text, **_kwargs: ["MSFT"] if "Microsoft" in text else [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: sorted(set(values) & {"MSFT"}))
    response_payload = {"items": [{
        "id": "article-1",
        "label": "markets",
        "relevance_score": 0.92,
        "confidence": 0.88,
        "reason": "Material cloud revenue news",
        "companies": [{"name": "Microsoft", "ticker": "MSFT", "evidence": "Microsoft revenue"}],
    }]}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
    ))

    results = calendar_relevance.classify_articles([{
        "id": "article-1", "title": "Microsoft reports", "summary": "Cloud revenue increased"
    }])

    assert results["article-1"]["relevance_label"] == "markets"
    assert results["article-1"]["related_tickers"] == ["MSFT"]
    assert results["article-1"]["related_companies"][0]["name"] == "Microsoft"


def test_short_record_indexes_restore_opaque_article_ids(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda _text, **_kwargs: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda _values: [])
    response_payload = {"items": [
        {"record_index": 0, "label": "unrelated", "relevance_score": 0.1, "confidence": 0.9},
        {"record_index": 1, "label": "business", "relevance_score": 0.8, "confidence": 0.9},
    ]}
    request = {}

    def post(_url, **kwargs):
        request.update(kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
        )

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    ids = ["64bf9ccfe945a15bf0147329ee3d50f6f1fea584", "2a889defc1ad55f1b4888e66f691e4404ca2db3d"]
    results = calendar_relevance.classify_articles([
        {"id": ids[0], "title": "First article"},
        {"id": ids[1], "title": "Second article"},
    ])

    assert set(results) == set(ids)
    assert results[ids[0]]["relevance_label"] == "unrelated"
    assert results[ids[1]]["relevance_label"] == "business"
    prompt = request["json"]["messages"][0]["content"]
    assert '"record_index": 0' in prompt
    assert ids[0] not in prompt


def test_only_confident_unrelated_result_is_removed(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "UNRELATED_CONFIDENCE_THRESHOLD", 0.8)

    assert calendar_relevance.should_remove({"relevance_label": "unrelated", "relevance_confidence": 0.9}) is True
    assert calendar_relevance.should_remove({"relevance_label": "unrelated", "relevance_confidence": 0.7}) is False
    assert calendar_relevance.should_remove({"relevance_label": "uncertain", "relevance_confidence": 1.0}) is False


def test_valid_but_contextually_unresolved_ticker_is_not_attached(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda _text, **_kwargs: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: sorted(set(values) & {"WEST"}))
    response_payload = {"items": [{
        "id": "article-2",
        "label": "finance",
        "relevance_score": 0.7,
        "confidence": 0.8,
        "reason": "Banking topic",
        "companies": [{"name": "West Bank", "ticker": "WEST", "evidence": "West Bank banks"}],
    }]}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
    ))

    result = calendar_relevance.classify_articles([{
        "id": "article-2", "title": "Banks in the West Bank", "summary": "Regional cash shortage"
    }])["article-2"]

    assert result["llm_ticker_candidates"] == ["WEST"]
    assert result["related_tickers"] == []


def test_each_company_is_resolved_without_other_article_companies(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    seen = []

    def resolve(value, **_kwargs):
        seen.append(value)
        return {"Novo Nordisk": ["NVO"], "Eli Lilly Inc": ["LLY"]}.get(value, [])

    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", resolve)
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: sorted(set(values)))
    response_payload = {"items": [{
        "id": "article-companies",
        "label": "business",
        "relevance_score": 0.9,
        "confidence": 0.9,
        "reason": "Company litigation",
        "companies": [
            {"name": "Novo Nordisk", "ticker": "NVO", "evidence": "Novo Nordisk filed"},
            {"name": "Eli Lilly Inc", "ticker": "LLY", "evidence": "against Eli Lilly"},
        ],
    }]}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
    ))

    result = calendar_relevance.classify_articles([{
        "id": "article-companies",
        "title": "Novo Nordisk sues Eli Lilly",
        "summary": "Litigation involving both companies.",
    }])["article-companies"]

    assert seen == ["Novo Nordisk", "Eli Lilly Inc"]
    assert result["related_tickers"] == ["LLY", "NVO"]
    assert result["related_companies"][0]["validated_tickers"] == ["NVO"]
    assert result["related_companies"][1]["validated_tickers"] == ["LLY"]


def test_explicit_company_relationship_adds_all_validated_tickers(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    monkeypatch.setattr(
        calendar_relevance,
        "resolve_company_tickers",
            lambda value, **_kwargs: {"NVIDIA": ["NVDA"], "Luminar Technologies": ["LAZR"]}.get(value, []),
    )
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: sorted(set(values)))
    response_payload = {"items": [{
        "id": "article-relationship",
        "label": "business",
        "relevance_score": 0.96,
        "confidence": 0.91,
        "reason": "Explicit technology partnership",
        "companies": [
            {"name": "NVIDIA", "ticker": "NVDA", "evidence": "NVIDIA platform"},
            {"name": "Luminar Technologies", "ticker": "LAZR", "evidence": "Luminar lidar"},
        ],
        "relationships": [{
            "company_a": "Luminar Technologies",
            "ticker_a": "LAZR",
            "relationship": "partnership",
            "company_b": "NVIDIA",
            "ticker_b": "NVDA",
            "evidence": "Luminar is working with NVIDIA",
            "materiality": "high",
        }],
    }]}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
    ))

    result = calendar_relevance.classify_articles([{
        "id": "article-relationship",
        "title": "Luminar works with NVIDIA on lidar",
        "summary": "Luminar is working with NVIDIA on autonomous vehicle lidar.",
    }])["article-relationship"]

    assert result["related_tickers"] == ["LAZR", "NVDA"]
    assert result["company_relationships"] == [{
        "company_a": "Luminar Technologies",
        "validated_tickers_a": ["LAZR"],
        "relationship": "partnership",
        "company_b": "NVIDIA",
        "validated_tickers_b": ["NVDA"],
        "evidence": "Luminar is working with NVIDIA",
        "materiality": "high",
    }]
    assert result["company_relationships_verified"] is True


def test_ungrounded_company_and_relationship_are_rejected(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda _value, **_kwargs: ["TSLA"])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: sorted(set(values)))
    response_payload = {"items": [{
        "id": "article-grounding",
        "label": "business",
        "relevance_score": 0.8,
        "confidence": 0.7,
        "reason": "Model guessed an unrelated company",
        "companies": [{"name": "Tesla", "ticker": "TSLA", "evidence": "Tesla announced"}],
        "relationships": [{
            "company_a": "Tesla",
            "ticker_a": "TSLA",
            "relationship": "supplier",
            "company_b": "Acme",
            "ticker_b": None,
            "evidence": "Tesla selected Acme",
            "materiality": "high",
        }],
    }]}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
    ))

    result = calendar_relevance.classify_articles([{
        "id": "article-grounding",
        "title": "Regional manufacturer reports quarterly sales",
        "summary": "The manufacturer reported higher quarterly sales.",
    }])["article-grounding"]

    assert result["related_companies"] == []
    assert result["related_tickers"] == []
    assert result["company_relationships"] == []
    assert result["company_relationships_verified"] is False


def test_verified_publisher_ticker_is_validated_and_attached(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda _value, **_kwargs: [])
    monkeypatch.setattr(
        calendar_relevance,
        "validate_security_symbols",
        lambda values: sorted(set(values) & {"AAPL"}),
    )
    response_payload = {"items": [{
        "id": "article-publisher-ticker",
        "label": "business",
        "relevance_score": 0.9,
        "confidence": 0.9,
        "reason": "Company product news",
        "companies": [],
        "relationships": [],
    }]}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
    ))

    result = calendar_relevance.classify_articles([{
        "id": "article-publisher-ticker",
        "title": "Apple announces a new product",
        "summary": "Apple announced a new product.",
        "publisher_tickers": ["AAPL", "US"],
        "content_verified": True,
        "headline_resolution_source": "canonical",
    }])["article-publisher-ticker"]

    assert result["related_tickers"] == ["AAPL"]
    assert result["article_verification_status"] == "canonical"


def test_non_english_verified_article_uses_english_translation(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "PROVIDER", "openai-compatible")
    monkeypatch.setattr(calendar_relevance, "API_BASE", "http://model.test/v1")
    monkeypatch.setattr(calendar_relevance, "MODEL", "test-model")
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda _value, **_kwargs: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: values)
    response_payload = {"items": [{
        "id": "article-spanish",
        "label": "markets",
        "relevance_score": 0.89,
        "confidence": 0.93,
        "reason": "Market-moving oil news",
        "headline_quality": "good",
        "display_headline": "Oil prices rise after supply disruption",
        "summary": "Oil prices rose after a supply disruption.",
        "source_language": "Spanish",
        "translated_to_english": True,
        "topics": ["energy markets"],
        "commodities": ["crude oil"],
        "companies": [],
        "relationships": [],
    }]}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": json.dumps(response_payload)}}]},
    ))

    result = calendar_relevance.classify_articles([{
        "id": "article-spanish",
        "title": "Suben los precios del petroleo tras interrupcion del suministro",
        "original_title": "Suben los precios del petroleo tras interrupcion del suministro",
        "summary": "Los precios del petroleo subieron.",
        "content_verified": True,
    }])["article-spanish"]

    assert result["display_headline"] == "Oil prices rise after supply disruption"
    assert result["original_headline"].startswith("Suben los precios")
    assert result["headline_source"] == "llm_translation"
    assert result["source_language"] == "Spanish"
    assert result["translated_to_english"] is True


def test_ollama_uses_native_chat_api(monkeypatch):
    monkeypatch.setattr(
        calendar_relevance,
        "_runtime_config",
        lambda: ("ollama", "http://ollama.test:11434", "qwen3:1.7b"),
    )
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda _text, **_kwargs: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda _values: [])
    monkeypatch.setattr(calendar_relevance, "CONTEXT_LENGTH", 16384)
    monkeypatch.setattr(calendar_relevance, "NUM_PREDICT", 4096)
    response_payload = {"items": [{
        "id": "article-3",
        "label": "business",
        "relevance_score": 0.9,
        "confidence": 0.9,
        "reason": "Company earnings",
        "headline_quality": "good",
        "display_headline": "Acme reports quarterly earnings",
        "summary": "Acme reported quarterly earnings.",
        "topics": ["earnings"],
        "commodities": ["crude oil"],
        "companies": [{"name": "Acme", "ticker": None, "evidence": "Acme reported"}],
    }]}
    request = {}

    def post(url, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": json.dumps(response_payload)}},
        )

    monkeypatch.setattr(calendar_relevance.requests, "post", post)

    result = calendar_relevance.classify_articles([{
        "id": "article-3",
        "title": "Acme reports quarterly earnings",
        "summary": "Acme reported quarterly earnings. \u4f01\u696d",
    }])["article-3"]

    assert request["url"] == "http://ollama.test:11434/api/chat"
    assert request["json"]["model"] == "qwen3:1.7b"
    assert request["json"]["think"] is False
    prompt = request["json"]["messages"][0]["content"]
    assert "\u4f01\u696d" in prompt
    assert "\\u4f01\\u696d" not in prompt
    assert request["json"]["format"] == calendar_relevance.OLLAMA_OUTPUT_SCHEMA
    assert request["json"]["options"] == {
        "temperature": 0,
        "num_ctx": 16384,
        "num_predict": 4096,
    }
    assert result["relevance_model"] == "qwen3:1.7b"
    assert result["related_commodities"] == ["Crude Oil"]


def test_untranslated_output_cannot_be_accepted_or_removed():
    result = {
        "source_language": "ja", "translated_to_english": True,
        "display_headline": "\u4f01\u696d\u306e\u8abf\u67fb", "llm_summary": "\u4f01\u696d\u306e\u8abf\u67fb",
        "relevance_label": "unrelated", "relevance_confidence": 0.99,
        "article_verification_status": "canonical", "entity_link_version": 999,
    }
    assert not calendar_relevance.should_remove(result)
    assert not calendar_relevance.is_automatically_accepted(result)
    assert calendar_relevance._translation_errors(result) == [
        "headline_not_english_script", "summary_not_english_script",
    ]
    result.update(display_headline="Toyota (\u30c8\u30e8\u30bf) reports earnings",
                  llm_summary="The company reported rising demand.")
    assert calendar_relevance._translation_errors(result) == []


@pytest.mark.parametrize("policy", ["business-english-v2", "business-english-v3", calendar_relevance.RELEVANCE_POLICY_VERSION])
def test_current_policy_requires_review_before_removal(policy):
    result = {"relevance_label": "unrelated", "relevance_confidence": 0.99,
              "relevance_policy_version": policy}
    assert not calendar_relevance.should_remove(result)
    result["relevance_reviewed"] = True
    assert calendar_relevance.should_remove(result)


def test_copied_latin_script_headline_is_unverified():
    title = "Una empresa privada lanza un servicio para comercios"
    result = {"source_language": "es", "translated_to_english": True,
              "display_headline": title, "original_headline": title,
              "llm_summary": "A company launched a service."}
    assert calendar_relevance._translation_errors(result) == ["headline_translation_unverified"]
    result["display_headline"] = "Private company launches a service for retailers"
    assert calendar_relevance._translation_errors(result) == []


def test_summary_rejects_invented_forecast_and_price_implications():
    article = {"title": "OPEC cuts 2026 global oil demand growth forecast to 400,000 bpd",
               "summary": "OPEC lowered its forecast to 400,000 barrels per day, down from an estimate of 1"}
    proposed = "OPEC cut its forecast from 1 million bpd, signaling tighter supply and upward pressure on prices."
    assert calendar_relevance._source_summary(article, proposed, False) == (article["title"], "source_headline_fallback")
    assert calendar_relevance._source_summary(article, article["summary"], False) == (article["title"], "source_headline_fallback")


def test_summary_rejects_ellipsis_and_accepts_complete_quoted_sentence():
    article = {"title": "Company reports earnings", "summary": "The company said revenue increased..."}
    assert calendar_relevance._source_summary(article, article["summary"], False)[1] == "source_headline_fallback"
    article["summary"] = 'The company said, "Revenue increased."'
    assert calendar_relevance._source_summary(article, article["summary"], False)[1] == "source_excerpt"


def test_summary_cannot_drop_a_negation_by_starting_mid_sentence():
    article = {"title": "Company reports earnings", "summary": "There is no evidence that revenue increased."}
    assert calendar_relevance._source_summary(article, "revenue increased.", False) == (article["title"], "source_headline_fallback")


@pytest.mark.parametrize("invented", [False, True])
def test_classifier_persists_only_guarded_english_summary(monkeypatch, invented):
    title = "OPEC cuts oil demand growth forecast to 400,000 bpd"
    proposed = "This implies tighter supply and higher oil prices." if invented else title
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda: ("ollama", "http://test", "test-model"))
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: [])
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        item = {"record_index": 0, "label": "markets", "confidence": 0.99,
                "display_headline": title, "summary": proposed, "source_language": "en"}
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": json.dumps({"items": [item]})}})

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    result = calendar_relevance.classify_articles([{"id": "oil", "title": title}])["oil"]
    assert result["llm_summary"] == title
    assert result["summary_source"] == ("source_headline_fallback" if invented else "source_excerpt")
    assert len(calls) == 1


def test_summary_preserves_source_words_without_joining_unrelated_passages():
    article = {"title": "Company reports earnings", "content_excerpt": "Sales rose.\nMargins declined."}
    assert calendar_relevance._source_summary(article, "Sales rose. Margins declined.", False) == (
        "Sales rose. Margins declined.", "source_excerpt")
    assert calendar_relevance._source_summary(article, "Margins rose.", False)[1] == "source_headline_fallback"
    assert calendar_relevance._source_summary({"title": "Prices fell 120 percent"}, "20 percent", False)[1] == "source_headline_fallback"


def test_long_or_empty_generated_summary_uses_headline_and_translation_is_not_certified():
    article = {"title": "Company reports earnings", "content_excerpt": "a " * 700}
    assert calendar_relevance._source_summary(article, article["content_excerpt"], False) == (article["title"], "source_headline_fallback")
    assert calendar_relevance._source_summary(article, "", False)[0] == article["title"]
    assert calendar_relevance._source_summary(article, "Translated statement.", True) == ("Translated statement.", "llm_translation_unverified")


@pytest.mark.parametrize("review_label", ["business", "unrelated"])
def test_unrelated_relevance_is_reviewed_once(monkeypatch, review_label):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: values)
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        result = {"record_index": 0, "label": review_label if len(calls) > 1 else "unrelated",
                  "confidence": 0.99, "display_headline": "A company announces a service",
                  "summary": "The company announced a service.", "source_language": "English"}
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": json.dumps({"items": [result]})}})

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    result = calendar_relevance._classify_articles_batch([{"id": "a", "title": "A company announces a service"}])["a"]
    assert len(calls) == 2
    assert result["relevance_label"] == review_label
    assert result["relevance_reviewed"] is True
    assert calendar_relevance.should_remove(result) is (review_label == "unrelated")


@pytest.mark.parametrize("repair_succeeds", [True, False])
@pytest.mark.parametrize("language,title", [("ja", "\u4f01\u696d\u306e\u8abf\u67fb"),
                                           ("es", "Una empresa publica una encuesta")])
def test_invalid_translation_gets_one_bounded_correction(monkeypatch, repair_succeeds, language, title):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda *a, **k: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: values)
    calls = []
    bad = {"record_index": 0, "label": "business", "confidence": 0.99,
           "source_language": language, "translated_to_english": True,
           "display_headline": title, "summary": title}
    good = {**bad, "label": "unrelated", "display_headline": "Company publishes a security survey",
            "summary": "The survey examines corporate security practices."}

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        result = good if repair_succeeds and len(calls) == 2 else bad
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": json.dumps({"items": [result]})}})

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    items = [{"id": "article", "title": title}]
    if repair_succeeds:
        result = calendar_relevance._classify_articles_batch(items)["article"]
        assert result["display_headline"] == good["display_headline"]
        assert result["relevance_label"] == "business"
    else:
        with pytest.raises(ValueError, match="English translation validation"):
            calendar_relevance._classify_articles_batch(items)
    assert len(calls) == 2
    assert "CORRECTION:" in calls[1]["messages"][0]["content"]
    assert '"previous_source_language":' in calls[1]["messages"][0]["content"]
    assert "headline_translation_unverified" in calls[1]["messages"][0]["content"]
    assert '"review_reason": "translation_correction"' in calls[1]["messages"][0]["content"]
    assert calls[0]["options"] == calls[1]["options"]


@pytest.mark.parametrize("bad_review", [False, True])
def test_relevance_review_preserves_previously_validated_display(monkeypatch, bad_review):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda *a, **k: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: values)
    title = "Vi\u1ec7t Nam detains a vessel after port collision"
    initial = {"record_index": 0, "label": "unrelated", "confidence": .9,
               "source_language": "English", "translated_to_english": False,
               "display_headline": title, "summary": title}
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        result = dict(initial)
        if len(calls) == 2 and bad_review:
            result["source_language"] = "Vietnamese"
            result["label"] = "business"
            result["summary"] = "Incorrect replacement summary"
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": json.dumps({"items": [result]})}})

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    result = calendar_relevance.classify_articles([{"id": "shipping", "title": title}])["shipping"]
    assert result["source_language"] == "English"
    assert result["translated_to_english"] is False
    assert result["llm_summary"] == title
    assert result["display_headline"] == title
    assert result["relevance_reviewed"] is True
    assert result["relevance_label"] == ("business" if bad_review else "unrelated")
    assert len(calls) == 2
    prompt = calls[1]["messages"][0]["content"]
    assert '"review_reason": "relevance_only"' in prompt
    assert '"previous_source_language": "English"' in prompt
    assert "Your previous output failed these translation checks" not in prompt
    assert calls[0]["options"] == calls[1]["options"]


def test_english_article_with_accented_name_corrects_language_not_headline(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda *a, **k: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: values)
    title = "Vi\u1ec7t Nam pavilion shows off wood products, furniture at US trade fair"
    summary = "Vietnamese furniture businesses are seeking to expand sales in the Americas."
    bad = {"record_index": 0, "label": "business", "confidence": .92,
           "source_language": "vi", "translated_to_english": True,
           "display_headline": title, "summary": summary}
    good = {**bad, "source_language": "English", "translated_to_english": False}
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        result = bad if len(calls) == 1 else good
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": json.dumps({"items": [result]})}})

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    result = calendar_relevance.classify_articles([{
        "id": "english", "title": title, "summary": summary, "content_verified": True,
    }])["english"]
    assert len(calls) == 2
    assert "NOT the country" in calls[0]["messages"][0]["content"]
    assert "Recheck source_language" in calls[1]["messages"][0]["content"]
    assert result["display_headline"] == title
    assert result["llm_summary"] == summary
    assert result["summary_source"] == "source_excerpt"
    assert result["source_language"] == "English"
    assert result["translated_to_english"] is False
    assert calendar_relevance._translation_errors(result) == []


def test_relevance_review_does_not_accept_invalid_prior_translation(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    title = "Una empresa publica una encuesta"
    previous = {"source_language": "Spanish", "translated_to_english": True,
                "display_headline": title, "original_headline": title, "llm_summary": title}
    response = {"record_index": 0, "label": "business", "source_language": "Spanish",
                "translated_to_english": True, "display_headline": title, "summary": title}
    monkeypatch.setattr(calendar_relevance.requests, "post", lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"message": {"content": json.dumps({"items": [response]})}}))
    with pytest.raises(calendar_relevance.PartialClassificationError):
        calendar_relevance._classify_articles_batch(
            [{"id": "a", "title": title}], _verification_pass=True, _validated_display={"a": previous},
        )


def test_classification_payload_caps_article_text_for_bounded_context():
    payload = calendar_relevance.prepare_classification_payload([{
        "id": "article-long",
        "summary": "s" * 1500,
        "article_text": "a" * 5000,
    }])[0]

    assert len(payload["summary"]) == 1000
    assert len(payload["content_excerpt"]) == 2500


def test_model_json_decoder_repairs_only_invalid_unicode_escapes():
    decoded = calendar_relevance._decode_model_json(
        r'{"items":[{"summary":"literal \\uZZZZ marker"}]}'
    )

    assert decoded["items"][0]["summary"] == r"literal \uZZZZ marker"


def test_model_json_decoder_rejects_truncated_structure():
    with pytest.raises(json.JSONDecodeError):
        calendar_relevance._decode_model_json('{"items":[{"summary":"unfinished')


def test_automated_acceptance_requires_grounding_confidence_and_certainty():
    accepted = {
        "entity_link_version": 2,
        "article_verification_status": "canonical",
        "relevance_confidence": 0.91,
        "relevance_label": "business",
    }

    assert calendar_relevance.is_automatically_accepted(accepted) is True
    assert calendar_relevance.is_automatically_accepted({**accepted, "article_verification_status": "unverified"}) is False
    assert calendar_relevance.is_automatically_accepted({**accepted, "relevance_confidence": 0.79}) is False
    assert calendar_relevance.is_automatically_accepted({**accepted, "relevance_label": "uncertain"}) is False
    assert calendar_relevance.is_automatically_accepted({**accepted, "entity_link_version": 1}) is False


def test_adaptive_classification_batches_pack_short_and_split_long_articles(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "CONTEXT_LENGTH", 4096)
    monkeypatch.setattr(calendar_relevance, "NUM_PREDICT", 1024)
    monkeypatch.setattr(calendar_relevance, "PROMPT_TOKEN_MARGIN", 256)
    monkeypatch.setattr(calendar_relevance, "PROMPT_FIXED_TOKENS", 1400)
    monkeypatch.setattr(calendar_relevance, "MAX_ITEMS_PER_REQUEST", 4)
    monkeypatch.setattr(calendar_relevance, "PROMPT_CHARS_PER_TOKEN", 3.2)

    short = [{"id": f"short-{index}", "title": "Brief headline"} for index in range(4)]
    assert [len(batch) for batch in calendar_relevance.classification_batches(short)] == [4]

    long = [{
        "id": f"long-{index}",
        "title": "Detailed headline",
        "summary": "s" * 1000,
        "article_text": "a" * 2500,
    } for index in range(4)]
    assert [len(batch) for batch in calendar_relevance.classification_batches(long)] == [1, 1, 1, 1]


def test_classification_runs_bounded_batches_and_combines_results(monkeypatch):
    monkeypatch.setattr(calendar_relevance, "MAX_ITEMS_PER_REQUEST", 2)
    monkeypatch.setattr(calendar_relevance, "MAX_CONCURRENT_REQUESTS", 2)
    monkeypatch.setattr(calendar_relevance, "CONTEXT_LENGTH", 16384)
    monkeypatch.setattr(calendar_relevance, "NUM_PREDICT", 4096)

    calls = []

    def classify_batch(items, *, allow_disabled=False):
        calls.append(([item["id"] for item in items], allow_disabled))
        return {item["id"]: {"relevance_label": "business"} for item in items}

    monkeypatch.setattr(calendar_relevance, "_classify_articles_batch", classify_batch)
    results = calendar_relevance.classify_articles(
        [{"id": f"article-{index}", "title": "Short"} for index in range(5)],
        allow_disabled=True,
    )

    assert sorted(len(ids) for ids, _allow_disabled in calls) == [1, 2, 2]
    assert all(allow_disabled for _ids, allow_disabled in calls)
    assert sorted(results) == [f"article-{index}" for index in range(5)]


@pytest.mark.parametrize("review_timeout", [False, True])
def test_translation_failure_preserves_valid_sibling_without_reinference(monkeypatch, review_timeout):
    monkeypatch.setattr(calendar_relevance, "_runtime_config", lambda **_: ("ollama", "http://test", "test-model"))
    monkeypatch.setattr(calendar_relevance, "resolve_company_tickers", lambda *a, **k: [])
    monkeypatch.setattr(calendar_relevance, "validate_security_symbols", lambda values: values)
    good = {"record_index": 0, "label": "business", "source_language": "English",
            "display_headline": "Company reports higher revenue", "summary": "Company reports higher revenue"}
    bad = {"record_index": 1, "label": "business", "source_language": "Spanish",
           "translated_to_english": True, "display_headline": "Una empresa publica una encuesta",
           "summary": "Una empresa publica una encuesta"}
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        if review_timeout and len(calls) > 1:
            raise calendar_relevance.requests.Timeout("review timed out")
        rows = [good, bad] if len(calls) == 1 else [{**bad, "record_index": 0}]
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": json.dumps({"items": rows})}})

    monkeypatch.setattr(calendar_relevance.requests, "post", post)
    with pytest.raises(calendar_relevance.PartialClassificationError) as caught:
        calendar_relevance.classify_articles([
            {"id": "good", "title": good["display_headline"]},
            {"id": "bad", "title": bad["display_headline"]},
        ])
    assert set(caught.value.results) == {"good"}
    assert caught.value.failed_ids == {"bad"}
    assert len(calls) == 2
    assert good["display_headline"] not in calls[1]["messages"][0]["content"]


@pytest.mark.parametrize("concurrency", [1, 2])
def test_partial_results_survive_across_model_batches(monkeypatch, concurrency):
    monkeypatch.setattr(calendar_relevance, "MAX_CONCURRENT_REQUESTS", concurrency)
    monkeypatch.setattr(calendar_relevance, "classification_batches", lambda items: [[i] for i in items])

    def classify(batch, **kwargs):
        key = batch[0]["id"]
        if key == "bad":
            raise calendar_relevance.PartialClassificationError({}, {key})
        return {key: {"relevance_label": "business"}}

    monkeypatch.setattr(calendar_relevance, "_classify_articles_batch", classify)
    with pytest.raises(calendar_relevance.PartialClassificationError) as caught:
        calendar_relevance.classify_articles([{"id": "a"}, {"id": "bad"}, {"id": "b"}])
    assert set(caught.value.results) == {"a", "b"}
    assert caught.value.failed_ids == {"bad"}


@pytest.mark.parametrize("worker_name", ["historical_article_worker", "calendar_relevance_worker"])
def test_worker_does_not_repeat_successful_inference_after_partial_error(monkeypatch, worker_name):
    from importlib import import_module
    worker = import_module("workers." + worker_name)
    calls = []

    def classify(items):
        calls.append(items)
        raise calendar_relevance.PartialClassificationError({"good": {"relevance_label": "business"}}, {"bad"})

    monkeypatch.setattr(worker, "classify_articles", classify)
    results, failed = worker._classify_resilient([{"id": "good"}, {"id": "bad"}])
    assert set(results) == {"good"}
    assert [item["id"] for item, _ in failed] == ["bad"]
    assert len(calls) == 1


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("error_type", [ValueError, calendar_relevance.requests.Timeout])
def test_request_failure_preserves_finished_batches(monkeypatch, concurrency, error_type):
    monkeypatch.setattr(calendar_relevance, "MAX_CONCURRENT_REQUESTS", concurrency)
    monkeypatch.setattr(calendar_relevance, "classification_batches", lambda items: [[i] for i in items])
    calls = []

    def classify(batch, **kwargs):
        key = batch[0]["id"]
        calls.append(key)
        if key == "bad":
            raise error_type("request failed")
        return {key: {"relevance_label": "business"}}

    monkeypatch.setattr(calendar_relevance, "_classify_articles_batch", classify)
    with pytest.raises(calendar_relevance.PartialClassificationError) as caught:
        calendar_relevance.classify_articles([{"id": "a"}, {"id": "bad"}, {"id": "b"}])
    assert set(caught.value.results) == ({"a"} if concurrency == 1 else {"a", "b"})
    assert caught.value.failed_ids == ({"bad", "b"} if concurrency == 1 else {"bad"})
    assert calls.count("a") == 1
    assert "request failed" in str(caught.value).lower()


@pytest.mark.parametrize("concurrency", [1, 2])
def test_total_request_failure_preserves_original_exception(monkeypatch, concurrency):
    monkeypatch.setattr(calendar_relevance, "MAX_CONCURRENT_REQUESTS", concurrency)
    monkeypatch.setattr(calendar_relevance, "classification_batches", lambda items: [[i] for i in items])
    error = calendar_relevance.requests.Timeout("offline")

    def classify(*args, **kwargs):
        raise error

    monkeypatch.setattr(calendar_relevance, "_classify_articles_batch", classify)
    with pytest.raises(calendar_relevance.requests.Timeout) as caught:
        calendar_relevance.classify_articles([{"id": "a"}, {"id": "b"}])
    assert caught.value is error
