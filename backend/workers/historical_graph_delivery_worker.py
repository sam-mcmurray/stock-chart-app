"""Deliver committed historical results to Neo4j without repeating inference."""
import json
import logging
import os
import time

import redis

from services import persistent_store
from services.article_graph import update_article_ai_enrichment
from services.calendar_relevance import should_remove
from services.news_rejection import deliver as deliver_rejection, queue_existing_rejections

logger = logging.getLogger(__name__)


def run_forever():
    client = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    delivered = 0
    while True:
        try:
            status = persistent_store.deliver_historical_graph_result(
                lambda url, result: deliver_rejection(url, result, client) if should_remove(result)
                else update_article_ai_enrichment(url, result, strict=True)
            )
            delivered += int(status == "delivered")
            client.set("news:historical:graph:heartbeat", json.dumps({
                "status": status, "ts": int(time.time()), "delivered": delivered,
            }))
            if status != "delivered":
                if status == "idle":
                    queue_existing_rejections()
                time.sleep(5)
        except Exception:
            logger.exception("Historical graph delivery failed; committed results retained")
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
