import logging

from services.article_graph import remove_superseded_article_nodes
from services.news_cleanup_queue import enqueue as enqueue_news_cleanup


logger = logging.getLogger(__name__)


def remove_unrelated_article(redis_client, item: dict) -> dict:
    event_id = str(item.get("id") or "")
    url = str(item.get("url") or "")
    removed = {"redis": False, "chroma": False, "graph": False}
    if event_id:
        pipe = redis_client.pipeline()
        pipe.zrem("news:recent:articles", event_id)
        pipe.delete(f"news:article:{event_id}")
        pipe.execute()
        removed["redis"] = True
    if event_id:
        removed["chroma"] = bool(enqueue_news_cleanup([event_id], client=redis_client))
    if url:
        try:
            removed["graph"] = remove_superseded_article_nodes([url]) >= 0
        except Exception as exc:
            logger.info("Unrelated article graph cleanup unavailable for %s: %s", event_id, exc)
    return removed
