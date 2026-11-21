import argparse
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from neo4j import GraphDatabase

from services import persistent_store

logger = logging.getLogger(__name__)

AUDIT_LOOKBACK_HOURS = int(os.getenv("NEWS_QUALITY_AUDIT_LOOKBACK_HOURS", "48"))
AUDIT_ISSUES_PER_TYPE = int(os.getenv("NEWS_QUALITY_AUDIT_ISSUES_PER_TYPE", "20"))
AUDIT_REVIEW_SAMPLES_PER_TYPE = int(os.getenv("NEWS_QUALITY_REVIEW_SAMPLES_PER_TYPE", "5"))
AUDIT_MAX_OPEN_REVIEWS = int(os.getenv("NEWS_QUALITY_MAX_OPEN_REVIEWS", "30"))
AUDIT_TIME = os.getenv("NEWS_QUALITY_AUDIT_TIME", "02:30")
AUDIT_TIMEZONE = os.getenv("NEWS_QUALITY_AUDIT_TIMEZONE", "America/Chicago")
AUDIT_RUN_ON_STARTUP = os.getenv("NEWS_QUALITY_RUN_ON_STARTUP", "false").lower() == "true"


def _driver():
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    auth = (user, password) if user and password else None
    return GraphDatabase.driver(os.getenv("NEO4J_URL", "bolt://neo4j:7687"), auth=auth)


def issue_key(issue_type: str, article_url: str) -> str:
    value = f"{issue_type}|{article_url}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def build_issue(issue_type: str, severity: str, record: dict, details: dict | None = None) -> dict:
    url = str(record.get("url") or "")
    return {
        "issue_key": issue_key(issue_type, url),
        "issue_type": issue_type,
        "severity": severity,
        "title": str(record.get("title") or "Untitled article")[:500],
        "article_url": url or None,
        "details": details or {},
    }


def _records(driver, query: str, **parameters) -> list[dict]:
    return [dict(record) for record in driver.execute_query(query, **parameters).records]


def run_audit(lookback_hours: int = AUDIT_LOOKBACK_HOURS) -> dict:
    started = time.time()
    cutoff = int(time.time()) - max(1, lookback_hours) * 3600
    future_cutoff = int(time.time()) + 86400
    issues = []

    with _driver() as driver:
        driver.execute_query(
            "CREATE INDEX article_updated_at IF NOT EXISTS FOR (a:Article) ON (a.updated_at)"
        )
        node_counts = _records(
            driver,
            """
            MATCH (a:Article)
            RETURN count(a) AS articles,
                   count(CASE WHEN a.updated_at >= datetime({epochSeconds: $cutoff}) THEN 1 END) AS recent_articles
            """,
            cutoff=cutoff,
        )[0]
        relationship_counts = {
            row["relationship"]: int(row["count"])
            for row in _records(
                driver,
                """
                MATCH ()-[r]->()
                WHERE type(r) IN ['REPORTS_EVENT', 'MENTIONS_SECURITY', 'LOCATED_IN']
                RETURN type(r) AS relationship, count(r) AS count
                """,
            )
        }

        system_query_specs = [
            (
                "gdelt_missing_event",
                "high",
                """
                MATCH (a:Article)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                  AND toLower(coalesce(a.provider, '')) CONTAINS 'gdelt'
                  AND NOT EXISTS { (a)-[:REPORTS_EVENT]->() }
                RETURN a.url AS url, a.title AS title, a.provider AS provider
                ORDER BY a.updated_at DESC LIMIT $limit
                """,
            ),
            (
                "future_timestamp",
                "high",
                """
                MATCH (a:Article)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                  AND coalesce(a.published_ts, 0) > $future_cutoff
                RETURN a.url AS url, a.title AS title, a.published_ts AS published_ts
                ORDER BY a.published_ts DESC LIMIT $limit
                """,
            ),
            (
                "missing_title",
                "medium",
                """
                MATCH (a:Article)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                  AND trim(coalesce(a.title, '')) = ''
                RETURN a.url AS url, a.title AS title, a.source AS source
                ORDER BY a.updated_at DESC LIMIT $limit
                """,
            ),
            (
                "ticker_overload",
                "medium",
                """
                MATCH (a:Article)-[:MENTIONS_SECURITY]->(s:Security)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                WITH a, collect(DISTINCT s.symbol) AS tickers
                WHERE size(tickers) > 4
                RETURN a.url AS url, a.title AS title, tickers
                ORDER BY size(tickers) DESC, a.updated_at DESC LIMIT $limit
                """,
            ),
            (
                "invalid_url",
                "medium",
                """
                MATCH (a:Article)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                  AND NOT (a.url STARTS WITH 'http://' OR a.url STARTS WITH 'https://')
                RETURN a.url AS url, a.title AS title
                ORDER BY a.updated_at DESC LIMIT $limit
                """,
            ),
        ]
        system_findings = {}
        for issue_type, _severity, query in system_query_specs:
            records = _records(
                driver,
                query,
                cutoff=cutoff,
                future_cutoff=future_cutoff,
                limit=max(1, AUDIT_ISSUES_PER_TYPE),
            )
            system_findings[issue_type] = {
                "sampled": len(records),
                "capped": len(records) >= AUDIT_ISSUES_PER_TYPE,
            }

        review_query_specs = [
            (
                "finance_relevance_review",
                "low",
                """
                MATCH (a:Article)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                  AND toLower(coalesce(a.provider, '')) CONTAINS 'gdelt'
                RETURN a.url AS url, a.title AS title,
                       {source: a.source, provider: a.provider} AS review_context
                ORDER BY a.updated_at DESC LIMIT $limit
                """,
            ),
            (
                "event_location_review",
                "low",
                """
                MATCH (a:Article)-[:LOCATED_IN]->(location:Location)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                RETURN a.url AS url, a.title AS title,
                       {event_location: location.name, source: a.source} AS review_context
                ORDER BY a.updated_at DESC LIMIT $limit
                """,
            ),
            (
                "ticker_label_review",
                "low",
                """
                MATCH (a:Article)-[:MENTIONS_SECURITY]->(security:Security)
                WHERE a.updated_at >= datetime({epochSeconds: $cutoff})
                WITH a, collect(DISTINCT security.symbol) AS tickers
                WHERE size(tickers) <= 4
                RETURN a.url AS url, a.title AS title,
                       {tickers: tickers, source: a.source} AS review_context
                ORDER BY a.updated_at DESC LIMIT $limit
                """,
            ),
        ]
        open_reviews = int(persistent_store.news_quality_issue_counts().get("open") or 0)
        remaining_slots = max(0, AUDIT_MAX_OPEN_REVIEWS - open_reviews)
        for index, (issue_type, severity, query) in enumerate(review_query_specs):
            if remaining_slots <= 0:
                break
            remaining_types = len(review_query_specs) - index
            sample_limit = min(
                max(1, AUDIT_REVIEW_SAMPLES_PER_TYPE),
                max(1, (remaining_slots + remaining_types - 1) // remaining_types),
            )
            records = _records(
                driver,
                query,
                cutoff=cutoff,
                future_cutoff=future_cutoff,
                limit=sample_limit,
            )
            for record in records:
                details = {key: value for key, value in record.items() if key not in {"url", "title"}}
                issues.append(build_issue(issue_type, severity, record, details))
            remaining_slots -= len(records)

    auto_resolved = persistent_store.auto_resolve_news_quality_issue_types(
        [issue_type for issue_type, _severity, _query in system_query_specs]
    )
    persistent_store.upsert_news_quality_issues(issues)
    report = {
        "started_ts": int(started),
        "completed_ts": int(time.time()),
        "duration_seconds": round(time.time() - started, 2),
        "lookback_hours": lookback_hours,
        "graph": {
            "articles": int(node_counts.get("articles") or 0),
            "recent_articles": int(node_counts.get("recent_articles") or 0),
            "relationships": relationship_counts,
        },
        "mentions": persistent_store.gdelt_mentions_stats(),
        "review_samples_added": len(issues),
        "max_open_reviews": AUDIT_MAX_OPEN_REVIEWS,
        "system_findings": system_findings,
        "auto_resolved_legacy_issues": auto_resolved,
        "issue_counts": persistent_store.news_quality_issue_counts(),
    }
    report["run_id"] = persistent_store.save_news_quality_report(report)
    return report


def next_audit_at(now: datetime | None = None, audit_time: str = AUDIT_TIME, timezone_name: str = AUDIT_TIMEZONE) -> datetime:
    zone = ZoneInfo(timezone_name)
    current = now.astimezone(zone) if now else datetime.now(zone)
    try:
        hour, minute = (int(value) for value in audit_time.split(":", 1))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
    except ValueError as exc:
        raise ValueError("NEWS_QUALITY_AUDIT_TIME must use HH:MM in 24-hour time") from exc
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target


def run_forever() -> None:
    logger.info("Starting news quality worker")
    if AUDIT_RUN_ON_STARTUP:
        try:
            report = run_audit()
            logger.info("Startup news quality audit complete: %s", report)
        except Exception:
            logger.exception("Startup news quality audit failed")
    while True:
        target = next_audit_at()
        delay = max(1.0, (target - datetime.now(target.tzinfo)).total_seconds())
        logger.info("Next news quality audit scheduled for %s", target.isoformat())
        time.sleep(delay)
        try:
            report = run_audit()
            logger.info("News quality audit complete: %s", report)
        except Exception:
            logger.exception("News quality audit failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit recent news graph quality")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--lookback-hours", type=int, default=AUDIT_LOOKBACK_HOURS)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if args.once:
        print(run_audit(max(1, args.lookback_hours)))
    else:
        run_forever()


if __name__ == "__main__":
    main()
