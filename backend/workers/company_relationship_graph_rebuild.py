import logging
import os

from services import persistent_store
from services.company_relationship_graph import upsert_company_relationship_batch

logger = logging.getLogger(__name__)
BATCH_SIZE = max(1, min(2000, int(os.getenv("COMPANY_RELATION_GRAPH_BATCH_SIZE", "500"))))


def run() -> dict:
    after_url = ""
    articles = 0
    relationships = 0
    while True:
        rows = persistent_store.list_company_relationship_graph_page(
            after_url=after_url,
            limit=BATCH_SIZE,
        )
        if not rows:
            break
        relationships += upsert_company_relationship_batch(rows)
        articles += len(rows)
        after_url = str(rows[-1].get("url") or "")
        logger.info(
            "Company relationship graph rebuild: articles=%s relationships=%s",
            articles,
            relationships,
        )
    return {"articles": articles, "relationships": relationships, "complete": True}


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger.info("Company relationship graph rebuild complete: %s", run())
