import pytest

from services.news_hype import (
    SCORING_SPECS, WATCH_PRIORITY_FEATURE_VERSION, build_news_hype_features,
    derive_watch_priority_features,
)


AS_OF = 2_000_000_000


def _article(title: str, age: int, domain: str = "example.com", **updates):
    article = {
        "title": title,
        "url": f"https://{domain}/{title.lower().replace(' ', '-')}",
        "published_ts": AS_OF - age,
        "tickers": ["NVDA"],
        "ticker_source": "accepted_llm_company_validated",
        "relevance_label": "finance",
        "provider": domain,
    }
    article.update(updates)
    return article


def test_hype_features_exclude_future_old_unrelated_and_unverified_evidence():
    result = build_news_hype_features(
        "NVDA",
        as_of_ts=AS_OF,
        articles=[
            _article("Nvidia raises guidance after earnings beat", 60),
            _article("Future report", -10),
            _article("Old report", 90_000),
            _article("Political report", 120, relevance_label="unrelated"),
            _article("Other company", 120, tickers=["AMD"]),
        ],
    )

    assert result["feature_version"] == "news-hype-v1"
    assert result["article_count"] == 1
    assert result["audit"]["excluded_future"] == 1
    assert result["audit"]["excluded_too_old"] == 1
    assert result["audit"]["excluded_relevance"] == 1
    assert result["audit"]["excluded_unverified_ticker"] == 1
    assert result["ranking_only"] is True
    assert result["scoring_spec"]["recency_weight"] == 0.25
    assert SCORING_SPECS["news-hype-v1"].feature_version == "news-hype-v1"


def test_hype_features_reject_unknown_formula_version():
    with pytest.raises(ValueError, match="Unknown news hype feature version"):
        build_news_hype_features("NVDA", feature_version="news-hype-v999")


def test_hype_features_deduplicate_coverage_and_measure_independent_domains():
    first = _article("Nvidia wins major supply contract", 120, "wire.example")
    duplicate = {
        **first,
        "url": first["url"] + "?utm_source=feed",
        "provider": "aggregator",
    }
    second = _article(
        "Nvidia supply agreement expands production", 180, "finance.example"
    )

    result = build_news_hype_features(
        "NVDA", as_of_ts=AS_OF, articles=[first, duplicate, second]
    )

    assert result["article_count"] == 2
    assert result["audit"]["deduplicated_articles"] == 1
    assert result["source_domain_count"] == 2
    assert result["source_breadth_score"] == 0.5
    assert result["attention_score"] > 0


def test_hype_features_are_event_time_safe_for_market_reaction():
    result = build_news_hype_features(
        "NVDA",
        as_of_ts=AS_OF,
        articles=[_article("Nvidia reports earnings beat", 100)],
        market_observations=[
            {"ts": AS_OF - 120, "price": 100},
            {"ts": AS_OF - 10, "price": 105},
            {"ts": AS_OF + 1, "price": 500},
        ],
    )

    assert result["market_reaction"]["available"] is True
    assert result["market_reaction"]["return_pct"] == 5
    assert result["market_reaction"]["latest_ts"] == AS_OF - 10


def test_hype_market_reaction_accepts_durable_candle_field_names():
    result = build_news_hype_features(
        "NVDA",
        as_of_ts=AS_OF,
        articles=[_article("Nvidia reports earnings beat", 100)],
        market_observations=[
            {"event_ts": AS_OF - 120, "close": 100},
            {"event_ts": AS_OF - 10, "close": 102},
        ],
    )

    assert result["market_reaction"]["available"] is True
    assert result["market_reaction"]["return_pct"] == 2


def test_gdelt_mention_velocity_uses_archive_timestamps_and_excludes_future():
    result = build_news_hype_features(
        "NVDA",
        as_of_ts=AS_OF,
        articles=[_article(
            "Nvidia reports new supply agreement",
            7_200,
            event_ids=["123"],
            gdelt_mentions=[
                {"event_id": "123", "url": "https://one.example/a", "mention_ts": AS_OF - 100},
                {"event_id": "123", "url": "https://two.example/b", "mention_ts": AS_OF - 200},
                {"event_id": "123", "url": "https://future.example/c", "mention_ts": AS_OF + 1},
            ],
        )],
    )

    assert result["velocity_basis"] == "gdelt_mentions"
    assert result["mention_count"] == 2
    assert result["recent_article_count"] == 0
    assert result["mention_velocity_ratio"] > 1


def test_repeated_old_headline_reduces_recent_novelty():
    repeated = build_news_hype_features(
        "NVDA",
        as_of_ts=AS_OF,
        articles=[
            _article("Nvidia announces major customer contract expansion", 7_200),
            _article(
                "Nvidia confirms major customer contract expansion today",
                120,
                domain="second.example",
            ),
        ],
    )
    novel = build_news_hype_features(
        "NVDA",
        as_of_ts=AS_OF,
        articles=[
            _article("Nvidia announces major customer contract expansion", 7_200),
            _article(
                "Nvidia receives new regulatory approval for accelerator sales",
                120,
                domain="second.example",
            ),
        ],
    )

    assert repeated["novelty_score"] < novel["novelty_score"]


def test_watch_priority_v2_preserves_v1_score_and_only_breaks_zero_score_ties():
    articles = [_article("Nvidia reports earnings beat and raises guidance", 60)]
    v1 = build_news_hype_features("NVDA", as_of_ts=AS_OF, articles=articles)
    v2 = build_news_hype_features(
        "NVDA", as_of_ts=AS_OF, articles=articles,
        feature_version=WATCH_PRIORITY_FEATURE_VERSION,
    )

    assert v2["long_hype_score"] == v1["long_hype_score"]
    assert v2["candidate_priority_tier"] == 1
    assert v2["candidate_priority_score"] == pytest.approx(1 + v1["long_hype_score"])
    assert v2["scoring_spec"]["ranking_score_field"] == "candidate_priority_score"
    assert "candidate_priority_score" not in v1


def test_watch_priority_v2_attention_cannot_outrank_positive_direction():
    neutral = build_news_hype_features(
        "NVDA", as_of_ts=AS_OF,
        articles=[_article("Nvidia hosts annual investor conference", 60)],
        feature_version=WATCH_PRIORITY_FEATURE_VERSION,
    )

    assert neutral["candidate_priority_tier"] == 0
    assert neutral["candidate_priority_score"] == neutral["attention_score"]
    assert neutral["candidate_priority_score"] <= 1


def test_watch_priority_can_be_derived_from_frozen_v1_live_snapshot():
    v1 = build_news_hype_features(
        "NVDA", as_of_ts=AS_OF,
        articles=[_article("Nvidia reports earnings beat and raises guidance", 60)],
    )
    derived = derive_watch_priority_features(v1)

    assert derived["feature_version"] == WATCH_PRIORITY_FEATURE_VERSION
    assert derived["long_hype_score"] == v1["long_hype_score"]
    assert derived["evidence_article_ids"] == v1["evidence_article_ids"]
    assert derived["derived_from_feature_version"] == "news-hype-v1"
    assert v1["feature_version"] == "news-hype-v1"


def test_watch_priority_derivation_rejects_nonbaseline_input():
    with pytest.raises(ValueError, match="requires a news-hype-v1 baseline"):
        derive_watch_priority_features({"feature_version": "other"})
