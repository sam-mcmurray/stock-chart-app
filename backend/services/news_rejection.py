"""Small rejection receipts prevent repeat inference without retaining article content."""
import hashlib

from services.calendar_relevance import should_remove
from services.article_dedup import article_dedup_id, canonicalize_url


def known_rejections(items):
    """Bounded indexed lookups, before fetching or inferring rejected URLs again."""
    from services import persistent_store as store
    urls = sorted({str(item.get("url") or "") for item in items} - {""})
    if not urls or not store.enabled():
        return {}
    keys = sorted(set(urls) | {canonicalize_url(url) for url in urls})
    rejected = {}
    with store.psycopg.connect(store.DATABASE_URL) as conn:
        for start in range(0, len(keys), 500):
            for url, result in conn.execute("""SELECT url,result FROM historical_article_enrichments
                    WHERE url = ANY(%s) AND relevance_label = 'unrelated'""", (keys[start:start + 500],)):
                if should_remove(result):
                    rejected[url] = receipt(result)
    return {url: rejected[url] if url in rejected else rejected[canonicalize_url(url)]
            for url in urls if url in rejected or canonicalize_url(url) in rejected}


def exclude_known_rejections(items):
    rejected = known_rejections(items)
    return [item for item in items if item.get("url") not in rejected]


def receipt(result):
    if not should_remove(result):
        return result
    keys = ("relevance_label", "relevance_confidence", "relevance_score", "relevance_reason",
            "relevance_source", "relevance_model", "relevance_policy_version",
            "relevance_reviewed", "relevance_classified_at", "financial_gate_rejected")
    return {**{k: result[k] for k in keys if k in result}, "rejected_receipt": True}


def deliver(url, result, client):
    from services import persistent_store as store
    from services.article_graph import remove_superseded_article_nodes
    from services.news_cleanup_queue import enqueue
    from services.calendar_relevance_queue import ENRICHMENTS_KEY
    # Deliveries are idempotent. Exceptions leave the existing durable outbox pending.
    ids = {hashlib.sha1(url.encode("utf-8")).hexdigest(), article_dedup_id({"url": url})}
    urls = list({url, canonicalize_url(url)})
    with store.psycopg.connect(store.DATABASE_URL) as conn:
        rows = conn.execute("SELECT id FROM calendar_events WHERE url = ANY(%s) AND details->>'calendar_kind' = 'market_news'", (urls,)).fetchall()
        ids.update(row[0] for row in rows)
        enqueue(list(ids), client=client)
        for identity in ids:
            client.zrem("news:recent:articles", identity)
            client.delete(f"news:article:{identity}")
            client.hdel(ENRICHMENTS_KEY, identity)
        conn.execute("DELETE FROM calendar_events WHERE id = ANY(%s)", ([row[0] for row in rows],))
        conn.execute("DELETE FROM news_enrichment_versions WHERE article_url = ANY(%s)", (urls,))
    if remove_superseded_article_nodes(urls) < 0:
        raise RuntimeError("Rejected article graph deletion failed")
    return True


def queue_existing_rejections(limit=50):
    """One resumable bounded pass over previously classified unrelated history."""
    from services import persistent_store as store
    key = "news_unrelated_cleanup:v1"
    with store.psycopg.connect(store.DATABASE_URL, row_factory=store.dict_row) as conn:
        conn.execute("INSERT INTO worker_checkpoints(name,value) VALUES (%s,'{}') ON CONFLICT DO NOTHING", (key,))
        checkpoint = conn.execute("SELECT value FROM worker_checkpoints WHERE name=%s FOR UPDATE", (key,)).fetchone()["value"]
        if checkpoint.get("id_version", 0) < 2:
            checkpoint.update(after_url="", complete=False, id_version=2)
        if checkpoint.get("complete"):
            return 0
        rows = conn.execute("""SELECT url,result FROM historical_article_enrichments
            WHERE url > %s AND relevance_label = 'unrelated'
            ORDER BY url LIMIT %s FOR UPDATE""", (checkpoint.get("after_url", ""), limit)).fetchall()
        queued = 0
        for row in rows:
            compact = receipt(row["result"])
            if not compact.get("rejected_receipt"):
                continue
            conn.execute("""UPDATE historical_article_enrichments SET original_title='', display_headline='',
                related_tickers='[]', related_companies='[]', company_relationships='[]',
                supporting_url=NULL, content_source=NULL, result=%s WHERE url=%s""", (store.jsonb(compact), row["url"]))
            conn.execute("""INSERT INTO historical_article_graph_outbox(url,result) VALUES (%s,%s)
                ON CONFLICT (url) DO UPDATE SET result=EXCLUDED.result, next_attempt_at=NOW()""",
                         (row["url"], store.jsonb(compact)))
            queued += 1
        checkpoint.update(after_url=rows[-1]["url"] if rows else checkpoint.get("after_url", ""),
                          complete=not rows, queued=int(checkpoint.get("queued", 0)) + queued)
        conn.execute("UPDATE worker_checkpoints SET value=%s,updated_at=NOW() WHERE name=%s",
                     (store.jsonb(checkpoint), key))
        return queued
