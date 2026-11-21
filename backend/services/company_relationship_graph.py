import logging
import os
from collections import defaultdict
from typing import Any

from neo4j import GraphDatabase

from services.ticker_utils import is_plausible_ticker

logger = logging.getLogger(__name__)

SCANNER_RELATIONSHIP_KINDS = {
    "partnership",
    "supplier",
    "customer",
    "licensing",
    "joint venture",
    "competitor",
}


def _driver():
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    auth = (user, password) if user and password else None
    return GraphDatabase.driver(os.getenv("NEO4J_URL", "bolt://neo4j:7687"), auth=auth)


def normalize_company_relationships(
    relationships: list[dict] | None,
    *,
    article_url: str = "",
    published_ts: int = 0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen = set()
    for relationship in relationships or []:
        if not isinstance(relationship, dict):
            continue
        kind = str(relationship.get("relationship") or "").strip().lower()
        if kind not in SCANNER_RELATIONSHIP_KINDS:
            continue
        tickers_a = {
            str(value or "").strip().upper()
            for value in relationship.get("validated_tickers_a") or []
            if is_plausible_ticker(value)
        }
        tickers_b = {
            str(value or "").strip().upper()
            for value in relationship.get("validated_tickers_b") or []
            if is_plausible_ticker(value)
        }
        for ticker_a in tickers_a:
            for ticker_b in tickers_b:
                if ticker_a == ticker_b:
                    continue
                key = (ticker_a, ticker_b, kind)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "ticker_a": ticker_a,
                    "ticker_b": ticker_b,
                    "kind": kind,
                    "company_a": str(relationship.get("company_a") or "")[:200],
                    "company_b": str(relationship.get("company_b") or "")[:200],
                    "evidence": str(relationship.get("evidence") or "")[:500],
                    "materiality": str(relationship.get("materiality") or "medium").lower()[:20],
                    "article_url": str(article_url or ""),
                    "published_ts": int(published_ts or 0),
                })
    return rows


def upsert_company_relationships(
    relationships: list[dict] | None,
    *,
    article_url: str = "",
    published_ts: int = 0,
    strict: bool = False,
) -> int:
    rows = normalize_company_relationships(
        relationships,
        article_url=article_url,
        published_ts=published_ts,
    )
    return _upsert_rows(rows, strict=strict)


def upsert_company_relationship_batch(articles: list[dict]) -> int:
    rows = []
    for article in articles:
        rows.extend(normalize_company_relationships(
            article.get("company_relationships") or [],
            article_url=str(article.get("url") or ""),
            published_ts=int(article.get("published_ts") or 0),
        ))
    return _upsert_rows(rows)


def _upsert_rows(rows: list[dict[str, Any]], *, strict: bool = False) -> int:
    if not rows:
        return 0
    try:
        with _driver() as driver:
            records, _summary, _keys = driver.execute_query(
                """
                UNWIND $rows AS row
                MATCH (a:Security {symbol: row.ticker_a})
                MATCH (b:Security {symbol: row.ticker_b})
                MERGE (a)-[relation:COMPANY_RELATION {kind: row.kind}]->(b)
                SET relation.company_a = row.company_a,
                    relation.company_b = row.company_b,
                    relation.evidence = row.evidence,
                    relation.materiality = row.materiality,
                    relation.article_url = row.article_url,
                    relation.last_seen_ts = CASE
                        WHEN coalesce(relation.last_seen_ts, 0) > row.published_ts
                        THEN relation.last_seen_ts ELSE row.published_ts END,
                    relation.updated_at = datetime()
                RETURN count(relation) AS count
                """,
                rows=rows,
            )
        return int(records[0]["count"] if records else 0)
    except Exception as exc:
        if strict:
            raise
        logger.warning("Company relationship graph update unavailable: %s", exc)
        return 0


def related_security_symbols(
    seed_symbols: list[str],
    *,
    max_per_seed: int = 5,
    limit: int = 250,
) -> dict[str, list[dict[str, Any]]]:
    seeds = sorted({
        str(symbol or "").strip().upper()
        for symbol in seed_symbols
        if is_plausible_ticker(symbol)
    })
    if not seeds:
        return {}
    materiality_rank = {"high": 3, "medium": 2, "low": 1}
    try:
        with _driver() as driver:
            records, _summary, _keys = driver.execute_query(
                """
                UNWIND $symbols AS symbol
                MATCH (seed:Security {symbol: symbol})-[relation:COMPANY_RELATION]-(related:Security)
                WHERE related.symbol <> seed.symbol
                  AND relation.kind IN $kinds
                  AND coalesce(related.is_primary_equity, true)
                  AND NOT coalesce(related.is_etf, false)
                RETURN seed.symbol AS seed, related.symbol AS symbol,
                       relation.kind AS kind, relation.materiality AS materiality,
                       relation.evidence AS evidence, relation.article_url AS article_url,
                       coalesce(relation.last_seen_ts, 0) AS last_seen_ts
                """,
                symbols=seeds,
                kinds=sorted(SCANNER_RELATIONSHIP_KINDS),
            )
    except Exception as exc:
        logger.info("Company relationship graph lookup unavailable: %s", exc)
        return {}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        row = dict(record)
        if is_plausible_ticker(row.get("symbol")):
            grouped[str(row.get("seed") or "")].append(row)
    result: dict[str, list[dict[str, Any]]] = {}
    remaining = max(0, int(limit))
    for seed in seeds:
        unique = {}
        for row in grouped.get(seed, []):
            symbol = str(row.get("symbol") or "").upper()
            current = unique.get(symbol)
            sort_key = (
                materiality_rank.get(str(row.get("materiality") or "").lower(), 0),
                int(row.get("last_seen_ts") or 0),
            )
            if current is None or sort_key > current[0]:
                unique[symbol] = (sort_key, row)
        ordered = [item[1] for item in sorted(unique.values(), key=lambda item: item[0], reverse=True)]
        selected = ordered[:min(max(0, int(max_per_seed)), remaining)]
        if selected:
            result[seed] = selected
            remaining -= len(selected)
        if remaining <= 0:
            break
    return result
