from routers import sentiment_api


def test_preview_combines_headline_and_summary(monkeypatch):
    captured = []
    monkeypatch.setattr(
        sentiment_api.sentiment_model_manager,
        "predict",
        lambda texts: captured.extend(texts) or {"results": [{"label": "positive", "score": 0.8}]},
    )

    result = sentiment_api.preview_sentiment(
        sentiment_api.SentimentPreview(headline="Revenue rises", summary="Guidance was raised."),
        _access_token="test",
    )

    assert captured == ["Revenue rises\nGuidance was raised."]
    assert result["label"] == "positive"


def test_model_load_proxies_explicit_label_map(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        sentiment_api.sentiment_model_manager,
        "load",
        lambda model, label_map: captured.update(model=model, label_map=label_map) or {"loaded": True},
    )

    result = sentiment_api.load_sentiment_model(
        sentiment_api.ModelLoad(
            model="owner/model",
            label_map={"LABEL_0": "negative", "LABEL_1": "neutral", "LABEL_2": "positive"},
        ),
        _access_token="test",
    )

    assert result["loaded"] is True
    assert captured["label_map"]["LABEL_0"] == "negative"


def test_catalog_failure_is_a_gateway_error(monkeypatch):
    monkeypatch.setattr(
        sentiment_api.sentiment_model_manager,
        "catalog",
        lambda _query: (_ for _ in ()).throw(RuntimeError("catalog unavailable")),
    )

    try:
        sentiment_api.sentiment_catalog("finance", _access_token="test")
    except sentiment_api.HTTPException as exc:
        assert exc.status_code == 502
    else:
        raise AssertionError("Catalog errors must not be returned as successful searches")


def test_evaluate_predicts_and_persists_expected_label(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        sentiment_api.sentiment_model_manager,
        "predict",
        lambda texts: {"results": [{
            "label": "positive",
            "confidence": 0.91,
            "score": 0.82,
            "probabilities": {"positive": 0.91, "neutral": 0.06, "negative": 0.03},
            "model": "owner/model",
        }]},
    )
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "save_sentiment_evaluation",
        lambda headline, summary, expected, prediction: captured.update(
            headline=headline, summary=summary, expected=expected, prediction=prediction,
        ) or {"correct": True, "model": prediction["model"]},
    )

    result = sentiment_api.evaluate_sentiment(
        sentiment_api.SentimentEvaluation(
            headline="Revenue rises",
            summary="Guidance was raised.",
            expected_label="positive",
        ),
        _access_token="test",
    )

    assert result["correct"] is True
    assert captured["expected"] == "positive"
    assert captured["prediction"]["confidence"] == 0.91


def test_evaluation_summary_uses_persistent_store(monkeypatch):
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "sentiment_evaluation_summary",
        lambda limit: {"available": True, "total": 3, "limit": limit},
    )

    result = sentiment_api.sentiment_evaluations(limit=12, _access_token="test")

    assert result == {"available": True, "total": 3, "limit": 12}


def test_benchmark_case_can_be_saved_without_running_model(monkeypatch):
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "save_sentiment_benchmark_case",
        lambda headline, summary, expected, source_url, note: {
            "case_key": "case-1", "headline": headline, "expected_label": expected,
        },
    )

    result = sentiment_api.save_benchmark_case(
        sentiment_api.SentimentBenchmarkCase(
            headline="Margins declined",
            summary="Costs increased.",
            expected_label="negative",
        ),
        _access_token="test",
    )

    assert result["case_key"] == "case-1"
    assert result["expected_label"] == "negative"


def test_benchmark_cases_can_be_exported(monkeypatch):
    cases = [{"case_key": "case-1", "headline": "Revenue rises", "expected_label": "positive"}]
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "list_sentiment_benchmark_cases",
        lambda limit, offset: cases if limit == 250 and offset == 20 else [],
    )

    result = sentiment_api.benchmark_cases(limit=250, offset=20, _access_token="test")

    assert result == {"cases": cases}


def test_benchmark_cases_are_bulk_imported(monkeypatch):
    captured = []
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "save_sentiment_benchmark_cases",
        lambda cases: captured.extend(cases) or [
            {"case_key": "case-1", **cases[0]},
            {"case_key": "case-2", **cases[1]},
        ],
    )
    body = sentiment_api.SentimentBenchmarkImport(cases=[
        sentiment_api.SentimentBenchmarkCase(
            headline="Revenue rises",
            summary="Guidance increased.",
            expected_label="positive",
            source_url="https://example.com/one",
            note="Reviewed filing",
        ),
        sentiment_api.SentimentBenchmarkCase(
            headline="Guidance held",
            summary="Outlook was unchanged.",
            expected_label="neutral",
        ),
    ])

    result = sentiment_api.import_benchmark_cases(body, _access_token="test")

    assert result["imported"] == 2
    assert captured[0]["note"] == "Reviewed filing"
    assert captured[1]["expected_label"] == "neutral"


def test_saved_benchmark_runs_as_one_batched_prediction(monkeypatch):
    cases = [
        {"headline": "Revenue rises", "summary": "Guidance increased", "expected_label": "positive"},
        {"headline": "Revenue unchanged", "summary": "Guidance held", "expected_label": "neutral"},
    ]
    predictions = [
        {"label": "positive", "model": "owner/model"},
        {"label": "neutral", "model": "owner/model"},
    ]
    saved = []
    monkeypatch.setattr(sentiment_api.persistent_store, "list_sentiment_benchmark_cases", lambda limit: cases)
    monkeypatch.setattr(
        sentiment_api.sentiment_model_manager,
        "predict",
        lambda texts: {"results": predictions},
    )
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "save_sentiment_evaluation",
        lambda headline, summary, expected, prediction: saved.append((headline, expected, prediction["label"])) or {},
    )
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "sentiment_evaluation_summary",
        lambda: {"total": len(saved)},
    )

    result = sentiment_api.run_sentiment_benchmark(limit=30, _access_token="test")

    assert result["evaluated"] == 2
    assert saved == [("Revenue rises", "positive", "positive"), ("Revenue unchanged", "neutral", "neutral")]


def test_delete_benchmark_case_removes_existing_case(monkeypatch):
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "delete_sentiment_benchmark_case",
        lambda case_key: case_key == "case-1",
    )

    result = sentiment_api.delete_benchmark_case("case-1", _access_token="test")

    assert result == {"deleted": True, "case_key": "case-1"}


def test_delete_benchmark_case_returns_not_found(monkeypatch):
    monkeypatch.setattr(
        sentiment_api.persistent_store,
        "delete_sentiment_benchmark_case",
        lambda _case_key: False,
    )

    try:
        sentiment_api.delete_benchmark_case("missing", _access_token="test")
    except sentiment_api.HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Missing benchmark cases must return 404")
