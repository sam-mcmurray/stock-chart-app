from unittest.mock import MagicMock

from services import historical_candidate_generator as generator


def _source():
    return {
        "version_key": "version-1",
        "record_type": "calendar_event",
        "record_id": "article-1",
        "article_url": "https://example.com/article",
        "published_ts": 1_000,
        "observed_ts": 1_100,
        "pipeline_version": "pipeline-v1",
        "result": {"related_tickers": ["NVDA", "AMD"]},
    }


def test_generator_pins_observed_cutoff_and_evidence_versions(monkeypatch):
    evidence = [{
        "enrichment_version_key": "evidence-v1",
        "event_ids": ["123"],
        "published_ts": 1_000,
        "related_tickers": ["NVDA"],
    }]
    list_evidence = MagicMock(return_value=evidence)
    monkeypatch.setattr(generator, "list_evidence_as_of", list_evidence)
    monkeypatch.setattr(generator, "_attach_mentions", lambda rows: rows)
    monkeypatch.setattr(
        generator.persistent_store,
        "list_market_observations_as_of",
        MagicMock(return_value=[{"event_ts": 1_050, "close": 10.0}]),
    )
    hype = {"feature_version": "news-hype-v1", "as_of_ts": 1_100, "long_hype_score": 0.7}
    monkeypatch.setattr(generator, "build_news_hype_features", MagicMock(return_value=hype))
    recorded = []

    def record(symbol, payload, **kwargs):
        recorded.append((symbol, payload, kwargs))
        return {"id": f"candidate-{symbol}", "payload": payload}

    monkeypatch.setattr(generator, "record_feature_snapshot", record)
    result_record = MagicMock()
    monkeypatch.setattr(generator, "record_source_result", result_record)

    candidates = generator.generate_from_source(_source())

    assert [row["id"] for row in candidates] == ["candidate-AMD", "candidate-NVDA"]
    assert all(call.kwargs["as_of_ts"] == 1_100 for call in list_evidence.call_args_list)
    assert all(
        call.kwargs["feature_version"] == "news-hype-v1"
        for call in generator.build_news_hype_features.call_args_list
    )
    nvda_payload = next(payload for symbol, payload, _ in recorded if symbol == "NVDA")
    assert nvda_payload["source_enrichment_version_key"] == "version-1"
    assert nvda_payload["evidence_enrichment_version_keys"] == ["evidence-v1"]
    assert nvda_payload["source_published_ts"] == 1_000
    assert nvda_payload["source_observed_ts"] == 1_100
    assert nvda_payload["snapshot_identity"] == "version-1:NVDA"
    assert nvda_payload["candidate_generator_version"] == generator.GENERATOR_VERSION
    assert nvda_payload["ranking_only"] is True
    assert nvda_payload["live_activation_eligible"] is False
    result_record.assert_called_once()
    assert result_record.call_args.kwargs["generator_version"] == generator.GENERATOR_VERSION


def test_future_generator_version_gets_distinct_snapshot_identity(monkeypatch):
    version = "news-hype-candidate-generator-test-v2"
    spec = generator.CandidateGeneratorSpec(version, "news-hype-v1")
    monkeypatch.setattr(generator, "GENERATOR_SPECS", {**generator.GENERATOR_SPECS, version: spec})
    monkeypatch.setattr(generator, "list_evidence_as_of", MagicMock(return_value=[]))
    monkeypatch.setattr(generator, "_attach_mentions", lambda rows: rows)
    monkeypatch.setattr(
        generator.persistent_store, "list_market_observations_as_of", MagicMock(return_value=[])
    )
    monkeypatch.setattr(
        generator, "build_news_hype_features",
        MagicMock(return_value={"feature_version": "news-hype-v1", "long_hype_score": 0}),
    )
    recorded = []
    monkeypatch.setattr(
        generator, "record_feature_snapshot",
        lambda symbol, payload, **kwargs: recorded.append(payload) or {"id": symbol},
    )
    monkeypatch.setattr(generator, "record_source_result", MagicMock())

    generator.generate_from_source(_source(), generator_version=version)

    assert all(payload["snapshot_identity"].endswith(f":{version}") for payload in recorded)


def test_watch_priority_generator_pins_v2_feature_and_experiment(monkeypatch):
    monkeypatch.setattr(generator, "list_evidence_as_of", MagicMock(return_value=[]))
    monkeypatch.setattr(generator, "_attach_mentions", lambda rows: rows)
    monkeypatch.setattr(
        generator.persistent_store, "list_market_observations_as_of", MagicMock(return_value=[])
    )
    feature_builder = MagicMock(return_value={
        "feature_version": "news-hype-v2-watch-priority",
        "candidate_priority_score": 0.5,
    })
    monkeypatch.setattr(generator, "build_news_hype_features", feature_builder)
    recorded = []
    monkeypatch.setattr(
        generator, "record_feature_snapshot",
        lambda symbol, payload, **kwargs: recorded.append(payload) or {"id": symbol},
    )
    monkeypatch.setattr(generator, "record_source_result", MagicMock())

    generator.generate_from_source(
        _source(), generator_version=generator.WATCH_PRIORITY_GENERATOR_VERSION
    )

    assert feature_builder.call_args.kwargs["feature_version"] == "news-hype-v2-watch-priority"
    assert all(
        payload["research_experiment_version"] == "news-hype-watch-priority-v2"
        for payload in recorded
    )
    assert all(payload["entry_eligible"] is False for payload in recorded)


def test_article_conversion_does_not_replace_observation_time():
    article = generator._article({
        "version_key": "v1", "record_id": "r1", "article_url": "https://example.com",
        "published_ts": 100, "observed_ts": 500, "pipeline_version": "p1",
        "result": {"related_tickers": ["F"], "display_headline": "Ford news"},
    })

    assert article["published_ts"] == 100
    assert article["observed_ts"] == 500
    assert article["enrichment_version_key"] == "v1"
    assert article["tickers"] == ["F"]
