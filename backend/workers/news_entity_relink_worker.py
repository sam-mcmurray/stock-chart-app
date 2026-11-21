import json
import logging
import os
import time

import redis

from services import persistent_store
from services.article_graph import update_article_entity_enrichments
from services.calendar_relevance import ENTITY_LINK_VERSION, is_automatically_accepted
from services.calendar_relevance_queue import (
    ACCEPTED_TICKERS_KEY,
    ENRICHMENTS_KEY,
    RECENT_ARTICLES_KEY,
)
from services.security_master import resolve_company_tickers, validate_security_symbols


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
HEARTBEAT_KEY = "news:entity-relink:heartbeat"
CHECKPOINT_NAME = f"news_entity_relink:v{ENTITY_LINK_VERSION}"
BATCH_SIZE = max(1, min(int(os.getenv("NEWS_ENTITY_RELINK_BATCH_SIZE", "500")), 5000))
r = redis.from_url(REDIS_URL, decode_responses=True)


def _resolve(name: str) -> list[str]:
    return validate_security_symbols(
        resolve_company_tickers(name, exact_company_name=True)
    )


def relink_result(result: dict, resolver=_resolve) -> dict:
    next_result = dict(result or {})
    companies = []
    tickers = set()
    for company in next_result.get("related_companies") or []:
        if not isinstance(company, dict):
            continue
        resolved = resolver(str(company.get("name") or "").strip())
        if not resolved:
            continue
        next_company = {**company, "validated_tickers": resolved}
        companies.append(next_company)
        tickers.update(resolved)

    relationships = []
    for relationship in next_result.get("company_relationships") or []:
        if not isinstance(relationship, dict):
            continue
        resolved_a = resolver(str(relationship.get("company_a") or "").strip())
        resolved_b = resolver(str(relationship.get("company_b") or "").strip())
        if not resolved_a and not resolved_b:
            continue
        relationships.append({
            **relationship,
            "validated_tickers_a": resolved_a,
            "validated_tickers_b": resolved_b,
        })
        tickers.update(resolved_a)
        tickers.update(resolved_b)

    next_result["related_companies"] = companies[:20]
    next_result["company_relationships"] = relationships[:20]
    next_result["related_tickers"] = sorted(tickers)[:20]
    next_result["company_identification_verified"] = bool(companies)
    next_result["company_relationships_verified"] = bool(relationships)
    next_result["entity_link_version"] = ENTITY_LINK_VERSION
    return next_result


def _heartbeat(status: str, **values) -> None:
    r.set(HEARTBEAT_KEY, json.dumps({"status": status, "ts": int(time.time()), **values}))


def sync_recent_projection() -> tuple[int, int]:
    article_rows = r.zrange(RECENT_ARTICLES_KEY, 0, -1, withscores=True)
    synced = 0
    ticker_scores: dict[str, float] = {}
    for start in range(0, len(article_rows), 500):
        batch = article_rows[start:start + 500]
        pipe = r.pipeline()
        for article_id, _score in batch:
            pipe.hget(f"news:article:{article_id}", "json")
        raw_articles = pipe.execute()
        articles = []
        urls = []
        for (article_id, score), raw in zip(batch, raw_articles):
            try:
                article = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                continue
            candidate_urls = []
            for value in [
                article.get("canonical_url"),
                article.get("url"),
                *(article.get("source_urls") or []),
            ]:
                value = str(value or "").strip()
                if value and value not in candidate_urls:
                    candidate_urls.append(value)
            if not candidate_urls:
                continue
            articles.append((article_id, float(score), candidate_urls))
            urls.extend(candidate_urls)
        results = persistent_store.historical_enrichment_results_by_url(urls)
        results.update(persistent_store.calendar_enrichment_results_by_url(urls))
        writes = {}
        for article_id, score, candidate_urls in articles:
            result = next((results[url] for url in candidate_urls if url in results), None)
            if not result:
                continue
            writes[article_id] = json.dumps(result)
            synced += 1
            if (
                is_automatically_accepted(result)
                and result.get("relevance_label") in {"finance", "business", "markets"}
            ):
                for ticker in result.get("related_tickers") or []:
                    symbol = str(ticker or "").strip().upper()
                    if symbol:
                        ticker_scores[symbol] = max(score, ticker_scores.get(symbol, 0))
        if writes:
            r.hset(ENRICHMENTS_KEY, mapping=writes)
    pipe = r.pipeline()
    pipe.delete(ACCEPTED_TICKERS_KEY)
    if ticker_scores:
        pipe.zadd(ACCEPTED_TICKERS_KEY, ticker_scores)
    pipe.execute()
    return synced, len(ticker_scores)


def run() -> dict:
    persistent_store.ensure_schema()
    state = persistent_store.load_worker_checkpoint(CHECKPOINT_NAME)
    before_published_ts = int(state.get("before_published_ts") or 0)
    before_url = str(state.get("before_url") or "")
    completed = int(state.get("completed") or 0)
    changed = int(state.get("changed") or 0)
    calendar_after_id = str(state.get("calendar_after_id") or "")
    calendar_completed = int(state.get("calendar_completed") or 0)
    while True:
        calendar_rows = persistent_store.list_calendar_entity_relink_page(
            entity_link_version=ENTITY_LINK_VERSION,
            after_id=calendar_after_id,
            limit=BATCH_SIZE,
        )
        if not calendar_rows:
            break
        calendar_updates = []
        for row in calendar_rows:
            result = relink_result(dict(row.get("details") or {}))
            calendar_updates.append({
                "id": row["id"],
                "url": row.get("url"),
                "published_ts": int((row.get("details") or {}).get("published_ts") or 0),
                "related_tickers": result["related_tickers"],
                "related_companies": result["related_companies"],
                "company_relationships": result["company_relationships"],
                "result": result,
            })
        graph_rows = [row for row in calendar_updates if row.get("url")]
        if graph_rows:
            update_article_entity_enrichments(graph_rows)
        persistent_store.update_calendar_entity_links(calendar_updates)
        calendar_after_id = str(calendar_rows[-1]["id"])
        calendar_completed += len(calendar_rows)
        state.update({
            "calendar_after_id": calendar_after_id,
            "calendar_completed": calendar_completed,
        })
        persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, state)
        _heartbeat(
            "relinking_calendar",
            calendar_completed=calendar_completed,
            batch_size=len(calendar_rows),
        )
    remaining = persistent_store.count_news_entity_relink_remaining(ENTITY_LINK_VERSION)
    total = completed + remaining

    while True:
        rows = persistent_store.list_news_entity_relink_page(
            entity_link_version=ENTITY_LINK_VERSION,
            before_published_ts=before_published_ts,
            before_url=before_url,
            limit=BATCH_SIZE,
        )
        if not rows:
            synced_recent, accepted_symbols = sync_recent_projection()
            state = {**state, "complete": True, "completed": completed, "changed": changed}
            persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, state)
            _heartbeat(
                "complete",
                total=total,
                completed=completed,
                remaining=0,
                changed=changed,
                synced_recent=synced_recent,
                accepted_symbols=accepted_symbols,
                calendar_completed=calendar_completed,
            )
            return state

        updates = []
        batch_changed = 0
        for row in rows:
            result = relink_result(dict(row.get("result") or {}))
            old_tickers = sorted(set(row.get("related_tickers") or []))
            batch_changed += int(old_tickers != result["related_tickers"])
            updates.append({
                "url": row["url"],
                "published_ts": int(row.get("published_ts") or 0),
                "related_tickers": result["related_tickers"],
                "related_companies": result["related_companies"],
                "company_relationships": result["company_relationships"],
                "result": result,
            })

        _heartbeat(
            "processing",
            total=total,
            completed=completed,
            remaining=max(0, total - completed),
            changed=changed,
            batch_size=len(rows),
            current_url=rows[0]["url"],
            current_published_ts=int(rows[0].get("published_ts") or 0),
        )
        update_article_entity_enrichments(updates)
        persistent_store.update_news_entity_links(updates)

        completed += len(rows)
        changed += batch_changed
        before_published_ts = int(rows[-1].get("published_ts") or 0)
        before_url = str(rows[-1]["url"])
        state = {
            "complete": False,
            "before_published_ts": before_published_ts,
            "before_url": before_url,
            "completed": completed,
            "changed": changed,
            "entity_link_version": ENTITY_LINK_VERSION,
            "calendar_after_id": calendar_after_id,
            "calendar_completed": calendar_completed,
        }
        persistent_store.save_worker_checkpoint(CHECKPOINT_NAME, state)
        _heartbeat(
            "ok",
            total=total,
            completed=completed,
            remaining=max(0, total - completed),
            changed=changed,
            batch_size=len(rows),
        )


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Starting one-shot News LLM entity relink worker")
    run()
