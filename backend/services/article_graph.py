import json
import logging
import os

from neo4j import GraphDatabase
from services.company_relationship_graph import upsert_company_relationships

logger = logging.getLogger(__name__)
_CONSTRAINTS_READY = False


def _driver():
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    auth = (user, password) if user and password else None
    return GraphDatabase.driver(os.getenv("NEO4J_URL", "bolt://neo4j:7687"), auth=auth)


def upsert_article_graph(articles: list[dict]) -> int:
    global _CONSTRAINTS_READY
    rows = [
        {
            "url": item.get("canonical_url") or item.get("url"),
            "title": item.get("title") or item.get("headline") or "",
            "summary": item.get("summary") or "",
            "published_ts": int(item.get("published_ts") or item.get("datetime") or 0),
            "source": item.get("source") or "unknown",
            "provider": ",".join(item.get("providers") or [item.get("provider") or "unknown"]),
            "source_urls": item.get("source_urls") or [item.get("url")],
            "tickers": sorted(set(item.get("tickers") or [])),
            "event_id": str(item.get("event_id") or ""),
            "event_ids": sorted(set(
                [str(value) for value in (item.get("event_ids") or []) if value]
                + ([str(item.get("event_id"))] if item.get("event_id") else [])
            )),
            "event_code": str(item.get("event_code") or ""),
            "location": item.get("location") or "",
            "actor1": item.get("actor1") or "",
            "actor2": item.get("actor2") or "",
        }
        for item in articles
        if item.get("canonical_url") or item.get("url")
    ]
    if not rows:
        return 0
    try:
        with _driver() as driver:
            if not _CONSTRAINTS_READY:
                driver.execute_query("CREATE CONSTRAINT article_url IF NOT EXISTS FOR (a:Article) REQUIRE a.url IS UNIQUE")
                driver.execute_query("CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:Event) REQUIRE e.id IS UNIQUE")
                driver.execute_query("CREATE CONSTRAINT location_name IF NOT EXISTS FOR (l:Location) REQUIRE l.name IS UNIQUE")
                _CONSTRAINTS_READY = True
            driver.execute_query(
                """
                UNWIND $rows AS row
                MERGE (a:Article {url: row.url})
                SET a.title = row.title, a.summary = row.summary, a.published_ts = row.published_ts,
                    a.source = row.source, a.provider = row.provider,
                    a.source_urls = row.source_urls, a.updated_at = datetime()
                WITH a, row
                OPTIONAL MATCH (a)-[old:MENTIONS_SECURITY]->(:Security)
                DELETE old
                WITH DISTINCT a, row
                FOREACH (_ IN CASE WHEN row.event_id = '' THEN [] ELSE [1] END |
                    MERGE (e:Event {id: row.event_id})
                    SET e.code = row.event_code, e.actor1 = row.actor1, e.actor2 = row.actor2
                    MERGE (a)-[:REPORTS_EVENT]->(e)
                )
                FOREACH (event_id IN row.event_ids |
                    MERGE (related:Event {id: event_id})
                    MERGE (a)-[:REPORTS_EVENT]->(related)
                )
                FOREACH (_ IN CASE WHEN row.location = '' OR toLower(row.location) CONTAINS 'unknown' THEN [] ELSE [1] END |
                    MERGE (l:Location {name: row.location})
                    MERGE (a)-[:LOCATED_IN]->(l)
                )
                """,
                rows=rows,
            )
            driver.execute_query(
                """
                UNWIND $rows AS row
                MATCH (a:Article {url: row.url})
                UNWIND row.tickers AS symbol
                MATCH (s:Security {symbol: symbol})
                MERGE (a)-[:MENTIONS_SECURITY]->(s)
                """,
                rows=rows,
            )
        return len(rows)
    except Exception as exc:
        logger.warning("Article graph update unavailable: %s", exc)
        return 0


def remove_superseded_article_nodes(urls: list[str], batch_size: int = 500) -> int:
    unique_urls = sorted({url for url in urls if url})
    if not unique_urls:
        return 0
    try:
        removed = 0
        with _driver() as driver:
            for start in range(0, len(unique_urls), max(1, batch_size)):
                batch = unique_urls[start:start + max(1, batch_size)]
                records, _summary, _keys = driver.execute_query(
                    """
                    UNWIND $urls AS url
                    MATCH (a:Article {url: url})
                    WITH collect(a) AS articles
                    WITH articles, size(articles) AS removed
                    FOREACH (article IN articles | DETACH DELETE article)
                    RETURN removed
                    """,
                    urls=batch,
                )
                removed += int(records[0]["removed"] if records else 0)
        return removed
    except Exception as exc:
        logger.warning("Superseded Article graph cleanup unavailable: %s", exc)
        return -1


def replace_article_security_edges(rows: list[dict]) -> int:
    payload = [
        {"url": row.get("url"), "tickers": sorted(set(row.get("tickers") or []))}
        for row in rows
        if row.get("url")
    ]
    if not payload:
        return 0
    with _driver() as driver:
        driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (a:Article {url: row.url})
            OPTIONAL MATCH (a)-[old:MENTIONS_SECURITY]->(:Security)
            DELETE old
            """,
            rows=payload,
        )
        driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (a:Article {url: row.url})
            UNWIND row.tickers AS symbol
            MATCH (s:Security {symbol: symbol})
            MERGE (a)-[:MENTIONS_SECURITY]->(s)
            """,
            rows=payload,
        )
    return len(payload)


def list_historical_articles(after_url: str = "", limit: int = 8) -> list[dict]:
    with _driver() as driver:
        records, _summary, _keys = driver.execute_query(
            """
            MATCH (a:Article)
            WHERE a.url > $after_url
            WITH a
            ORDER BY a.url
            LIMIT $limit
            OPTIONAL MATCH (a)-[:REPORTS_EVENT]->(event:Event)
            WITH a, [event_id IN collect(DISTINCT event.id)
                     WHERE event_id IS NOT NULL][..20] AS event_ids
            RETURN a.url AS url, a.title AS title, a.summary AS summary,
                   a.published_ts AS published_ts, a.source AS source,
                   a.provider AS provider, a.source_urls AS source_urls,
                   event_ids
            ORDER BY a.url
            """,
            after_url=str(after_url or ""),
            limit=max(1, min(int(limit), 100)),
        )
    return [dict(record) for record in records]


def ensure_historical_article_index() -> None:
    with _driver() as driver:
        driver.execute_query(
            "CREATE RANGE INDEX article_published_url IF NOT EXISTS "
            "FOR (a:Article) ON (a.published_ts, a.url)"
        )


def list_historical_articles_newest(
    before_published_ts: int = 0,
    before_url: str = "",
    limit: int = 8,
    min_published_ts: int = 0,
) -> list[dict]:
    """Seek dated articles by index; retain legacy semantics at the undated tail."""
    before = max(0, int(before_published_ts or 0))
    minimum = max(0, int(min_published_ts or 0))
    size = max(1, min(int(limit), 100))
    branches = []
    if before:
        branches.append("""
            MATCH (a:Article)
            WHERE a.published_ts = $before AND a.published_ts >= $minimum
              AND a.url < $url
            RETURN a ORDER BY a.published_ts DESC, a.url DESC LIMIT $limit
        """)
    branches.append("""
        MATCH (a:Article)
        WHERE a.published_ts >= $minimum AND a.url IS NOT NULL
    """ + (" AND a.published_ts < $before " if before else "") + """
        RETURN a ORDER BY a.published_ts DESC, a.url DESC LIMIT $limit
    """)
    with _driver() as driver:
        records, _, _ = driver.execute_query(
            "CALL () { " + " UNION ALL ".join(branches) + " } " + """
            WITH a ORDER BY a.published_ts DESC, a.url DESC LIMIT $limit
            OPTIONAL MATCH (a)-[:REPORTS_EVENT]->(event:Event)
            WITH a, [event_id IN collect(DISTINCT event.id)
                     WHERE event_id IS NOT NULL][..20] AS event_ids
            RETURN a.url AS url, a.title AS title, a.summary AS summary,
                   a.published_ts AS published_ts, a.source AS source,
                   a.provider AS provider, a.source_urls AS source_urls, event_ids
            ORDER BY published_ts DESC, url DESC
            """,
            before=before, minimum=max(1, minimum), url=str(before_url or ""), limit=size,
        )
    if len(records) < size and minimum == 0:
        return _list_historical_articles_newest_legacy(before, before_url, size, minimum)
    return [dict(record) for record in records]


def _list_historical_articles_newest_legacy(
    before_published_ts: int = 0,
    before_url: str = "",
    limit: int = 8,
    min_published_ts: int = 0,
) -> list[dict]:
    """Page Article nodes newest-to-oldest with a stable timestamp/URL cursor."""
    with _driver() as driver:
        records, _summary, _keys = driver.execute_query(
            """
            MATCH (a:Article)
            USING SCAN a:Article
            WITH a, coalesce(a.published_ts, 0) AS published_ts
            WHERE $before_published_ts = 0
               OR published_ts < $before_published_ts
               OR (published_ts = $before_published_ts AND a.url < $before_url)
            WITH a, published_ts
            WHERE $min_published_ts = 0 OR published_ts >= $min_published_ts
            ORDER BY published_ts DESC, a.url DESC
            LIMIT $limit
            OPTIONAL MATCH (a)-[:REPORTS_EVENT]->(event:Event)
            WITH a, published_ts,
                 [event_id IN collect(DISTINCT event.id)
                  WHERE event_id IS NOT NULL][..20] AS event_ids
            RETURN a.url AS url, a.title AS title, a.summary AS summary,
                   published_ts, a.source AS source, a.provider AS provider,
                   a.source_urls AS source_urls, event_ids
            ORDER BY published_ts DESC, a.url DESC
            """,
            before_published_ts=max(0, int(before_published_ts or 0)),
            before_url=str(before_url or ""),
            limit=max(1, min(int(limit), 100)),
            min_published_ts=max(0, int(min_published_ts or 0)),
        )
    return [dict(record) for record in records]


def newest_historical_article_timestamp() -> int:
    """Return the newest plausible Article timestamp for a fixed backfill boundary."""
    with _driver() as driver:
        records, _summary, _keys = driver.execute_query(
            """
            MATCH (a:Article)
            WITH coalesce(a.published_ts, 0) AS published_ts
            WHERE published_ts <= timestamp() / 1000 + 86400
            RETURN max(published_ts) AS published_ts
            """
        )
    return int(records[0]["published_ts"] if records and records[0]["published_ts"] else 0)


def historical_article_partition_size(
    min_published_ts: int = 0,
    before_published_ts: int = 0,
    before_url: str = "",
) -> int:
    """Count records remaining after a worker's stable timestamp/URL cursor."""
    with _driver() as driver:
        records, _summary, _keys = driver.execute_query(
            """
            MATCH (a:Article)
            WITH coalesce(a.published_ts, 0) AS published_ts, a.url AS url
            WHERE ($min_published_ts = 0 OR published_ts >= $min_published_ts)
              AND ($before_published_ts = 0
                   OR published_ts < $before_published_ts
                   OR (published_ts = $before_published_ts AND url < $before_url))
            RETURN count(*) AS total
            """,
            min_published_ts=max(0, int(min_published_ts or 0)),
            before_published_ts=max(0, int(before_published_ts or 0)),
            before_url=str(before_url or ""),
        )
    return int(records[0]["total"] if records else 0)


def list_qdrant_rebuild_articles(after_url: str = "", limit: int = 100) -> list[dict]:
    """Page publisher articles that are not represented by the GDELT Event vectors."""
    with _driver() as driver:
        records, _summary, _keys = driver.execute_query(
            """
            MATCH (a:Article)
            WHERE a.url > $after_url
              AND NOT (a)-[:REPORTS_EVENT]->(:Event)
            WITH a
            ORDER BY a.url
            LIMIT $limit
            OPTIONAL MATCH (a)-[:MENTIONS_SECURITY]->(security:Security)
            WITH a, [symbol IN collect(DISTINCT security.symbol)
                     WHERE symbol IS NOT NULL] AS graph_tickers
            RETURN a.url AS url,
                   coalesce(a.ai_headline, a.title, '') AS title,
                   a.summary AS summary,
                   a.published_ts AS published_ts,
                   a.source AS source,
                   a.provider AS provider,
                   CASE WHEN size(coalesce(a.ai_tickers, [])) > 0
                        THEN a.ai_tickers ELSE graph_tickers END AS tickers
            ORDER BY a.url
            """,
            after_url=str(after_url or ""),
            limit=max(1, min(int(limit), 1000)),
        )
    return [dict(record) for record in records]


def update_article_ai_enrichment(url: str, result: dict, *, strict: bool = False) -> bool:
    with _driver() as driver:
        records, _summary, _keys = driver.execute_query(
            """
            MATCH (article:Article {url: $url})
            WHERE NOT $strict OR coalesce(article.ai_classified_at, 0) <= $classified_at
            SET article.ai_relevance = $relevance,
                article.ai_relevance_score = $relevance_score,
                article.ai_confidence = $confidence,
                article.ai_headline = $headline,
                article.ai_verification_status = $verification_status,
                article.ai_content_source = $content_source,
                article.ai_supporting_url = $supporting_url,
                article.ai_tickers = $tickers,
                article.ai_companies_json = $companies_json,
                article.ai_relationships_json = $relationships_json,
                article.ai_model = $model,
                article.ai_classified_at = $classified_at,
                article.updated_at = datetime()
            RETURN article.url AS url
            """,
            url=url,
            strict=strict,
            relevance=result.get("relevance_label"),
            relevance_score=float(result.get("relevance_score") or 0),
            confidence=float(result.get("relevance_confidence") or 0),
            headline=result.get("display_headline") or "",
            verification_status=result.get("article_verification_status") or "unverified",
            content_source=result.get("article_content_source") or "",
            supporting_url=result.get("supporting_article_url") or "",
            tickers=result.get("related_tickers") or [],
            companies_json=json.dumps(result.get("related_companies") or [], separators=(",", ":")),
            relationships_json=json.dumps(
                result.get("company_relationships") or [],
                separators=(",", ":"),
            ),
            model=result.get("relevance_model") or "",
            classified_at=int(result.get("relevance_classified_at") or 0),
        )
        if strict and not records:
            newer, _, _ = driver.execute_query(
                "MATCH (a:Article {url: $url}) WHERE a.ai_classified_at > $classified_at RETURN a.url AS url",
                url=url, classified_at=int(result.get("relevance_classified_at") or 0),
            )
            if newer:
                return True
    if not records:
        return False
    replace_article_security_edges([{"url": url, "tickers": result.get("related_tickers") or []}])
    upsert_company_relationships(
        result.get("company_relationships") or [],
        article_url=url,
        published_ts=int(result.get("published_ts") or 0),
        strict=strict,
    )
    return True


def update_article_entity_enrichments(rows: list[dict]) -> int:
    payload = [{
        "url": row.get("url"),
        "tickers": sorted(set(row.get("related_tickers") or [])),
        "companies_json": json.dumps(row.get("related_companies") or [], separators=(",", ":")),
        "relationships_json": json.dumps(row.get("company_relationships") or [], separators=(",", ":")),
        "entity_link_version": int((row.get("result") or {}).get("entity_link_version") or 0),
    } for row in rows if row.get("url")]
    if not payload:
        return 0
    with _driver() as driver:
        driver.execute_query(
            """
            UNWIND $rows AS row
            MATCH (article:Article {url: row.url})
            SET article.ai_tickers = row.tickers,
                article.ai_companies_json = row.companies_json,
                article.ai_relationships_json = row.relationships_json,
                article.ai_entity_link_version = row.entity_link_version,
                article.updated_at = datetime()
            """,
            rows=payload,
        )
    replace_article_security_edges(payload)
    for row in rows:
        if not row.get("url"):
            continue
        result = row.get("result") or {}
        upsert_company_relationships(
            row.get("company_relationships") or result.get("company_relationships") or [],
            article_url=str(row["url"]),
            published_ts=int(row.get("published_ts") or result.get("published_ts") or 0),
        )
    return len(payload)
