import argparse
import json
import logging
import os
from pathlib import Path

from neo4j import GraphDatabase

from services.article_graph import replace_article_security_edges
from services.security_master import resolve_company_tickers

logger = logging.getLogger(__name__)


def _driver():
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    auth = (user, password) if user and password else None
    return GraphDatabase.driver(os.getenv("NEO4J_URL", "bolt://neo4j:7687"), auth=auth)


def labels_for_article(article: dict, resolver=resolve_company_tickers) -> list[str]:
    actors = " ".join(str(value or "") for value in article.get("actors") or [])
    return resolver(f"{article.get('title') or ''} {actors}")


def _load_state(path: Path, reset: bool) -> dict:
    if reset or not path.exists():
        return {"last_url": "", "processed": 0, "changed": 0, "removed_edges": 0, "added_edges": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_url": "", "processed": 0, "changed": 0, "removed_edges": 0, "added_edges": 0}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def cleanup_security_labels(
    *,
    batch_size: int = 500,
    state_path: str = "/data/security_label_cleanup_state.json",
    reset: bool = False,
    dry_run: bool = False,
    max_articles: int = 0,
) -> dict:
    path = Path(state_path)
    state = _load_state(path, reset)
    start_processed = int(state.get("processed") or 0)
    limit = max(1, batch_size)

    with _driver() as driver:
        while True:
            records = driver.execute_query(
                """
                MATCH (a:Article)-[:MENTIONS_SECURITY]->(current:Security)
                WHERE a.url > $last_url
                OPTIONAL MATCH (a)-[:REPORTS_EVENT]->(event:Event)
                WITH a, collect(DISTINCT current.symbol) AS tickers,
                     collect(DISTINCT event.actor1) + collect(DISTINCT event.actor2) AS actors
                RETURN a.url AS url, a.title AS title, tickers, actors
                ORDER BY url
                LIMIT $limit
                """,
                last_url=str(state.get("last_url") or ""),
                limit=limit,
            ).records
            if not records:
                state["complete"] = True
                if not dry_run:
                    _save_state(path, state)
                break

            updates = []
            for record in records:
                article = dict(record)
                current = sorted(set(article.get("tickers") or []))
                resolved = labels_for_article(article)
                state["processed"] = int(state.get("processed") or 0) + 1
                if current != resolved:
                    state["changed"] = int(state.get("changed") or 0) + 1
                    state["removed_edges"] = int(state.get("removed_edges") or 0) + len(set(current) - set(resolved))
                    state["added_edges"] = int(state.get("added_edges") or 0) + len(set(resolved) - set(current))
                    updates.append({"url": article["url"], "tickers": resolved})
                state["last_url"] = article["url"]
                if max_articles and int(state["processed"]) - start_processed >= max_articles:
                    break

            if updates and not dry_run:
                replace_article_security_edges(updates)
            if not dry_run:
                state["complete"] = False
                _save_state(path, state)
            logger.info(
                "Security label cleanup processed=%s changed=%s cursor=%s",
                state["processed"],
                state["changed"],
                state["last_url"],
            )
            if max_articles and int(state["processed"]) - start_processed >= max_articles:
                break

    state["dry_run"] = dry_run
    return state


def main():
    parser = argparse.ArgumentParser(description="Recalculate existing Neo4j article security labels")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--state-path", default="/data/security_label_cleanup_state.json")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-articles", type=int, default=0)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    print(json.dumps(cleanup_security_labels(
        batch_size=args.batch_size,
        state_path=args.state_path,
        reset=args.reset,
        dry_run=args.dry_run,
        max_articles=args.max_articles,
    ), indent=2))


if __name__ == "__main__":
    main()
