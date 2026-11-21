from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import math
import re
import time
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from services.article_dedup import canonicalize_url, deduplicate_articles
from services.decision_engine import classify_catalysts, news_sentiment


from services.news_hype_versions import FEATURE_VERSION, WATCH_PRIORITY_FEATURE_VERSION


@dataclass(frozen=True)
class HypeScoringSpec:
    feature_version: str
    recency_weight: float
    source_breadth_weight: float
    velocity_weight: float
    novelty_weight: float
    materiality_weight: float
    catalyst_direction_multiplier: float
    ranking_score_field: str
    ranking_strategy: str


SCORING_SPECS = MappingProxyType({
    FEATURE_VERSION: HypeScoringSpec(
        feature_version=FEATURE_VERSION,
        recency_weight=0.25,
        source_breadth_weight=0.20,
        velocity_weight=0.20,
        novelty_weight=0.15,
        materiality_weight=0.20,
        catalyst_direction_multiplier=2.0,
        ranking_score_field="long_hype_score",
        ranking_strategy="positive_direction_only",
    ),
    WATCH_PRIORITY_FEATURE_VERSION: HypeScoringSpec(
        feature_version=WATCH_PRIORITY_FEATURE_VERSION,
        recency_weight=0.25,
        source_breadth_weight=0.20,
        velocity_weight=0.20,
        novelty_weight=0.15,
        materiality_weight=0.20,
        catalyst_direction_multiplier=2.0,
        ranking_score_field="candidate_priority_score",
        ranking_strategy="positive_direction_then_attention_tiebreak",
    ),
})
ACCEPTED_RELEVANCE = frozenset({"finance", "business", "markets"})
ACCEPTED_VERIFICATION = frozenset(
    {"canonical", "publisher", "mention", "gdelt_mention"}
)
TOKEN_RE = re.compile(r"[a-z0-9]+")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _timestamp_seconds(value: Any) -> int:
    number = _integer(value)
    if 10_000_000_000_000 <= number <= 99_999_999_999_999:
        try:
            return int(
                datetime.strptime(str(number), "%Y%m%d%H%M%S")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            return 0
    if number > 10_000_000_000:
        return number // 1000
    return number


def _strings(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)] if value not in (None, "") else []


def _tickers(article: dict[str, Any]) -> set[str]:
    values = (
        _strings(article.get("tickers"))
        + _strings(article.get("related_tickers"))
        + _strings(article.get("symbols"))
    )
    return {value.strip().upper() for value in values if value.strip()}


def _is_verified(article: dict[str, Any]) -> bool:
    if article.get("ticker_source") == "accepted_llm_company_validated":
        return True
    status = str(article.get("article_verification_status") or "").lower()
    try:
        link_version = int(article.get("entity_link_version") or 0)
    except (TypeError, ValueError):
        link_version = 0
    return status in ACCEPTED_VERIFICATION and link_version >= 2


def _title_tokens(article: dict[str, Any]) -> set[str]:
    title = article.get("title") or article.get("headline") or ""
    return set(TOKEN_RE.findall(str(title).lower()))


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _source_domains(articles: list[dict[str, Any]]) -> list[str]:
    domains: set[str] = set()
    for article in articles:
        urls = (
            _strings(article.get("source_urls"))
            + _strings(article.get("url"))
            + [
                str(mention.get("url") or "")
                for mention in article.get("gdelt_mentions") or []
                if isinstance(mention, dict)
            ]
        )
        for raw_url in urls:
            try:
                host = (urlsplit(canonicalize_url(raw_url)).hostname or "").lower()
            except ValueError:
                host = ""
            if host:
                domains.add(host.removeprefix("www."))
    return sorted(domains)


def _market_reaction(
    observations: list[dict[str, Any]], *, first_article_ts: int, as_of_ts: int
) -> dict[str, Any]:
    available = sorted(
        (
            {
                "ts": _integer(row.get("ts") or row.get("event_ts")),
                "price": _number(row.get("price") or row.get("close")),
            }
            for row in observations or []
        ),
        key=lambda row: row["ts"],
    )
    available = [
        row for row in available if 0 < row["ts"] <= as_of_ts and row["price"] > 0
    ]
    before = [row for row in available if row["ts"] <= first_article_ts]
    after = [row for row in available if row["ts"] >= first_article_ts]
    if not before or not after:
        return {
            "available": False,
            "return_pct": None,
            "baseline_ts": None,
            "latest_ts": available[-1]["ts"] if available else None,
        }
    baseline = before[-1]
    latest = after[-1]
    return {
        "available": True,
        "return_pct": round((latest["price"] / baseline["price"] - 1) * 100, 6),
        "baseline_ts": baseline["ts"],
        "latest_ts": latest["ts"],
    }


def build_news_hype_features(
    ticker: str,
    *,
    articles: list[dict[str, Any]] | None = None,
    as_of_ts: int | None = None,
    market_observations: list[dict[str, Any]] | None = None,
    lookback_seconds: int = 86_400,
    recent_window_seconds: int = 3_600,
    feature_version: str = FEATURE_VERSION,
) -> dict[str, Any]:
    """Build auditable event-time features without allowing future evidence."""
    try:
        scoring = SCORING_SPECS[feature_version]
    except KeyError as exc:
        raise ValueError(f"Unknown news hype feature version: {feature_version}") from exc
    symbol = str(ticker or "").strip().upper()
    cutoff = int(as_of_ts or time.time())
    lookback = max(3_600, int(lookback_seconds))
    recent_window = max(300, min(int(recent_window_seconds), lookback))
    audit = {
        "input_articles": len(articles or []),
        "excluded_future": 0,
        "excluded_too_old": 0,
        "excluded_missing_time": 0,
        "excluded_unverified_ticker": 0,
        "excluded_relevance": 0,
    }
    eligible: list[dict[str, Any]] = []
    for article in articles or []:
        published_ts = _timestamp_seconds(
            article.get("published_ts") or article.get("datetime")
        )
        if published_ts <= 0:
            audit["excluded_missing_time"] += 1
            continue
        if published_ts > cutoff:
            audit["excluded_future"] += 1
            continue
        if published_ts < cutoff - lookback:
            audit["excluded_too_old"] += 1
            continue
        if symbol not in _tickers(article) or not _is_verified(article):
            audit["excluded_unverified_ticker"] += 1
            continue
        if str(article.get("relevance_label") or "").lower() not in ACCEPTED_RELEVANCE:
            audit["excluded_relevance"] += 1
            continue
        eligible.append({**article, "published_ts": published_ts})

    evidence = sorted(
        deduplicate_articles(eligible),
        key=lambda article: int(article.get("published_ts") or 0),
    )
    recent_start = cutoff - recent_window
    recent = [row for row in evidence if int(row.get("published_ts") or 0) >= recent_start]
    prior = [row for row in evidence if int(row.get("published_ts") or 0) < recent_start]
    domains = _source_domains(evidence)
    providers = sorted({
        provider.lower()
        for row in evidence
        for provider in (_strings(row.get("providers")) + _strings(row.get("provider")))
        if provider
    })

    recency_score = 0.0
    if evidence:
        newest_age = max(0, cutoff - int(evidence[-1].get("published_ts") or 0))
        recency_score = math.exp(-newest_age / max(recent_window, 1))
    source_breadth_score = min(1.0, len(domains) / 4.0)
    mention_points: dict[tuple[str, str], int] = {}
    for row in eligible:
        event_ids = _strings(row.get("event_ids")) + _strings(row.get("event_id"))
        default_event_id = event_ids[0] if event_ids else ""
        for mention in row.get("gdelt_mentions") or []:
            if not isinstance(mention, dict):
                continue
            mention_ts = _timestamp_seconds(mention.get("mention_ts"))
            event_id = str(mention.get("event_id") or default_event_id)
            url = canonicalize_url(str(mention.get("url") or ""))
            if cutoff - lookback <= mention_ts <= cutoff and (event_id or url):
                mention_points[(event_id, url)] = mention_ts
    velocity_timestamps = list(mention_points.values()) or [
        int(row.get("published_ts") or 0) for row in evidence
    ]
    velocity_basis = "gdelt_mentions" if mention_points else "article_publications"
    recent_velocity_count = sum(ts >= recent_start for ts in velocity_timestamps)
    prior_velocity_count = len(velocity_timestamps) - recent_velocity_count
    elapsed_prior_hours = max((lookback - recent_window) / 3600, 1 / 60)
    baseline_rate = prior_velocity_count / elapsed_prior_hours
    recent_rate = recent_velocity_count / max(recent_window / 3600, 1 / 60)
    velocity_ratio = recent_rate / max(baseline_rate, 0.25)
    velocity_score = min(1.0, math.log1p(velocity_ratio) / math.log(9))

    novelty_values: list[float] = []
    chronological_tokens: list[set[str]] = [_title_tokens(row) for row in prior]
    for row in recent:
        tokens = _title_tokens(row)
        maximum_similarity = max(
            (_similarity(tokens, previous) for previous in chronological_tokens),
            default=0.0,
        )
        novelty_values.append(1.0 - maximum_similarity)
        chronological_tokens.append(tokens)
    novelty_score = (
        sum(novelty_values) / len(novelty_values) if novelty_values else 0.0
    )

    catalyst = classify_catalysts(evidence)
    sentiment = news_sentiment(evidence)
    materiality_score = min(1.0, abs(_number(catalyst.get("score"))) / 0.5)
    attention_score = (
        scoring.recency_weight * recency_score
        + scoring.source_breadth_weight * source_breadth_score
        + scoring.velocity_weight * velocity_score
        + scoring.novelty_weight * novelty_score
        + scoring.materiality_weight * materiality_score
    )
    directional_score = max(
        -1.0,
        min(
            1.0,
            _number(sentiment.get("score"))
            + scoring.catalyst_direction_multiplier * _number(catalyst.get("score")),
        ),
    )
    long_hype_score = attention_score * max(0.0, directional_score)
    candidate_priority_tier = 1 if long_hype_score > 0 else 0
    candidate_priority_score = (
        1.0 + long_hype_score if candidate_priority_tier else attention_score
    )
    first_article_ts = int(evidence[0].get("published_ts") or 0) if evidence else 0

    return {
        "feature_version": scoring.feature_version,
        "scoring_spec": {
            "recency_weight": scoring.recency_weight,
            "source_breadth_weight": scoring.source_breadth_weight,
            "velocity_weight": scoring.velocity_weight,
            "novelty_weight": scoring.novelty_weight,
            "materiality_weight": scoring.materiality_weight,
            "catalyst_direction_multiplier": scoring.catalyst_direction_multiplier,
            "ranking_score_field": scoring.ranking_score_field,
            "ranking_strategy": scoring.ranking_strategy,
        },
        "ticker": symbol,
        "as_of_ts": cutoff,
        "lookback_seconds": lookback,
        "article_count": len(evidence),
        "recent_article_count": len(recent),
        "source_domain_count": len(domains),
        "provider_count": len(providers),
        "source_domains": domains,
        "providers": providers,
        "recency_score": round(recency_score, 6),
        "source_breadth_score": round(source_breadth_score, 6),
        "mention_velocity_ratio": round(velocity_ratio, 6),
        "mention_velocity_score": round(velocity_score, 6),
        "mention_count": len(mention_points),
        "velocity_basis": velocity_basis,
        "novelty_score": round(novelty_score, 6),
        "catalyst_materiality_score": round(materiality_score, 6),
        "attention_score": round(attention_score, 6),
        "directional_score": round(directional_score, 6),
        "long_hype_score": round(long_hype_score, 6),
        **({
            "candidate_priority_tier": candidate_priority_tier,
            "candidate_priority_score": round(candidate_priority_score, 6),
        } if feature_version == WATCH_PRIORITY_FEATURE_VERSION else {}),
        "catalyst": catalyst,
        "sentiment": sentiment,
        "market_reaction": _market_reaction(
            market_observations or [],
            first_article_ts=first_article_ts,
            as_of_ts=cutoff,
        ) if evidence else {
            "available": False,
            "return_pct": None,
            "baseline_ts": None,
            "latest_ts": None,
        },
        "evidence_article_ids": [
            str(row.get("dedup_id") or row.get("url") or "") for row in evidence
        ],
        "audit": {**audit, "deduplicated_articles": len(eligible) - len(evidence)},
        "ranking_only": True,
    }


def derive_watch_priority_features(
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Derive the preregistered v2 rank from a frozen v1 live snapshot."""
    if str(baseline.get("feature_version") or "") != FEATURE_VERSION:
        raise ValueError("Watch-priority v2 requires a news-hype-v1 baseline")
    scoring = SCORING_SPECS[WATCH_PRIORITY_FEATURE_VERSION]
    long_hype_score = _number(baseline.get("long_hype_score"))
    attention_score = _number(baseline.get("attention_score"))
    tier = 1 if long_hype_score > 0 else 0
    priority = 1.0 + long_hype_score if tier else attention_score
    result = deepcopy(baseline)
    result.update({
        "feature_version": WATCH_PRIORITY_FEATURE_VERSION,
        "scoring_spec": {
            "recency_weight": scoring.recency_weight,
            "source_breadth_weight": scoring.source_breadth_weight,
            "velocity_weight": scoring.velocity_weight,
            "novelty_weight": scoring.novelty_weight,
            "materiality_weight": scoring.materiality_weight,
            "catalyst_direction_multiplier": scoring.catalyst_direction_multiplier,
            "ranking_score_field": scoring.ranking_score_field,
            "ranking_strategy": scoring.ranking_strategy,
        },
        "candidate_priority_tier": tier,
        "candidate_priority_score": round(priority, 6),
        "derived_from_feature_version": FEATURE_VERSION,
        "ranking_only": True,
    })
    return result
