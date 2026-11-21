# backend/workers/knowledge_graph_worker.py
import logging
import os
import time
from typing import Dict, Any, List

from neo4j import GraphDatabase
from transformers import pipeline, AutoTokenizer, AutoModelForTokenClassification
from relik.client import RelikClient

from news_sources.rss_yahoo import load_yahoo_rss
from services.ticker_utils import extract_tickers

logging.basicConfig(level=logging.INFO)


class KnowledgeGraphWorker:
    def __init__(self):
        self.neo4j_url = os.getenv("NEO4J_URL", "bolt://localhost:7687")
        self.neo4j_driver = None
        self.running = False

        logging.info("Loading NER model...")
        tokenizer = AutoTokenizer.from_pretrained("dslim/bert-base-NER")
        model = AutoModelForTokenClassification.from_pretrained("dslim/bert-base-NER")
        self.ner_pipeline = pipeline("ner", model=model, tokenizer=tokenizer)
        logging.info("NER model loaded.")

        logging.info("Loading Relik model...")
        self.relik_client = RelikClient.from_pretrained("sapienzanlp/relik-relation-extraction-nyt-large")
        logging.info("Relik model loaded.")

    def _ensure_neo4j_connection(self):
        if self.neo4j_driver is None:
            try:
                self.neo4j_driver = GraphDatabase.driver(self.neo4j_url)
                self.neo4j_driver.verify_connectivity()
                logging.info("Successfully connected to Neo4j.")
            except Exception as e:
                logging.error(f"Failed to connect to Neo4j: {e}")
                self.neo4j_driver = None

    def _extract_entities_and_relationships(self, text: str) -> Dict[str, List[Dict[str, Any]]]:
        ner_results = self.ner_pipeline(text)

        grouped_ner_entities = []
        for entity in ner_results:
            if grouped_ner_entities and entity["word"].startswith("##") and grouped_ner_entities[-1]["entity_group"] == entity["entity"]:
                grouped_ner_entities[-1]["word"] += entity["word"][2:]
            elif grouped_ner_entities and entity["index"] == grouped_ner_entities[-1]["index"] + 1 and grouped_ner_entities[-1]["entity_group"] == entity["entity"]:
                grouped_ner_entities[-1]["word"] += " " + entity["word"]
                grouped_ner_entities[-1]["index"] += 1
            else:
                grouped_ner_entities.append(
                    {
                        "word": entity["word"],
                        "entity_group": entity["entity"],
                        "index": entity["index"],
                    }
                )

        formatted_entities = []
        for entity in grouped_ner_entities:
            entity_type = "Unknown"
            if entity["entity_group"] == "PER":
                entity_type = "Person"
            elif entity["entity_group"] == "ORG":
                entity_type = "Company"
            elif entity["entity_group"] == "LOC":
                entity_type = "Location"

            formatted_entities.append({"name": entity["word"].replace(" ", ""), "type": entity_type})

        relik_output = self.relik_client.extract(text=text)

        extracted_entities_map = {ent["name"]: ent for ent in formatted_entities if ent["name"]}
        for relik_ent in relik_output.entities:
            entity_type = "Unknown"
            if relik_ent.type == "PER":
                entity_type = "Person"
            elif relik_ent.type == "ORG":
                entity_type = "Company"
            elif relik_ent.type in ("GPE", "LOC"):
                entity_type = "Location"

            key = relik_ent.text.replace(" ", "")
            if key and key not in extracted_entities_map:
                extracted_entities_map[key] = {"name": key, "type": entity_type}

        final_entities = list(extracted_entities_map.values())

        formatted_relationships = []
        for rel in relik_output.relations:
            source_name = rel.head_entity.text.replace(" ", "")
            target_name = rel.tail_entity.text.replace(" ", "")
            relation_type = rel.type.upper()
            if source_name in extracted_entities_map and target_name in extracted_entities_map:
                formatted_relationships.append(
                    {"source": source_name, "target": target_name, "type": relation_type}
                )

        return {"entities": final_entities, "relationships": formatted_relationships}

    def _insert_into_neo4j(self, data: Dict[str, Any], article: Dict[str, Any]):
        if not self.neo4j_driver:
            return

        url = article.get("url")
        title = article.get("headline") or article.get("title") or ""
        ts = article.get("datetime") or article.get("published_ts") or int(time.time())
        tickers = article.get("tickers") or []

        with self.neo4j_driver.session() as session:
            session.run(
                """
                MERGE (a:Article {url: $url})
                SET a.title = $title, a.published_ts = $ts
                """,
                url=url,
                title=title,
                ts=ts,
            )

            for t in tickers:
                session.run(
                    """
                    MERGE (t:Ticker {symbol: $sym})
                    WITH t
                    MATCH (a:Article {url: $url})
                    MERGE (a)-[:MENTIONS]->(t)
                    """,
                    sym=t.upper(),
                    url=url,
                )

            for entity in data["entities"]:
                if entity["type"] == "Unknown":
                    continue
                session.run(
                    f"MERGE (n:{entity['type']} {{name: $name}}) ON CREATE SET n.name = $name",
                    name=entity["name"],
                )
                session.run(
                    """
                    MATCH (a:Article {url: $url}), (n {name: $name})
                    MERGE (a)-[:MENTIONS]->(n)
                    """,
                    url=url,
                    name=entity["name"],
                )

            for rel in data["relationships"]:
                session.run(
                    f"""
                    MATCH (a {{name: $source}}), (b {{name: $target}})
                    MERGE (a)-[r:{rel['type']}]->(b)
                    """,
                    source=rel["source"],
                    target=rel["target"],
                )

    def run_once(self):
        self._ensure_neo4j_connection()
        if not self.neo4j_driver:
            logging.error("Cannot run without a Neo4j connection.")
            return

        logging.info("Fetching news articles from Yahoo RSS.")
        news_articles = load_yahoo_rss()
        logging.info(f"Fetched {len(news_articles)} articles.")

        for article in news_articles:
            headline = article.get("headline", "")
            if not headline:
                continue
            article["tickers"] = article.get("tickers") or extract_tickers(headline)

            data = self._extract_entities_and_relationships(headline)
            if data["entities"] or data["relationships"]:
                self._insert_into_neo4j(data, article)
                logging.info(f"Inserted data into Neo4j for headline: {headline}")

    def run_forever(self):
        self.running = True
        while self.running:
            self.run_once()
            time.sleep(300)

    def stop(self):
        self.running = False
        if self.neo4j_driver:
            self.neo4j_driver.close()


if __name__ == "__main__":
    worker = KnowledgeGraphWorker()
    worker.run_once()
