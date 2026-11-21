# backend/routers/rag_api.py
import logging
import os
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query
from neo4j import GraphDatabase
from pydantic import BaseModel, Field, root_validator
import numpy as np
import time
from datetime import datetime, timezone
from services import persistent_store
from services.qdrant_store import (
    GDELT_EVENT_COLLECTION,
    NEWS_COLLECTION,
    embed_texts,
    get_store,
)
from services.security_master import resolve_company_tickers

logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/rag", tags=["rag"])

NEO4J_URL = os.getenv("NEO4J_URL", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

EMBEDDING_DIM_OUT = int(os.getenv("RAG_SIGNAL_EMBED_DIM", "16"))
NORMALIZE_EMBEDDING = os.getenv("RAG_SIGNAL_EMBED_NORM", "true").lower() == "true"
EMBED_PROJ_SEED = int(os.getenv("RAG_SIGNAL_EMBED_SEED", "1234"))
RAG_RECENCY_DAYS = int(os.getenv("RAG_RECENCY_DAYS", "0"))  # 0 disables recency filtering
RAG_CACHE_TTL = int(os.getenv("RAG_CACHE_TTL_SECONDS", "60"))

# LLM config: default to deterministic summary; supports OpenAI-compatible endpoints (e.g., Ollama / llama.cpp / vLLM)
RAG_LLM_PROVIDER = os.getenv("RAG_LLM_PROVIDER", "").lower()  # "openai", "openai-compatible", "ollama"
DEFAULT_OLLAMA_BASE = "http://localhost:11434/v1"
LLM_API_BASE = os.getenv("RAG_LLM_API_BASE") or os.getenv("OPENAI_API_BASE")
LLM_API_KEY = os.getenv("RAG_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")  # optional for local servers
LLM_MODEL = os.getenv("RAG_LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

class RagQuery(BaseModel):
    question: Optional[str] = Field(None, description="Natural language question")
    # Back-compat with older clients that sent {\"query\": \"...\"}
    query: Optional[str] = Field(None, description="Alias for question (legacy)")
    tickers: List[str] = Field(default_factory=list, description="Optional list of tickers to focus on")
    top_k: int = Field(default=5, ge=1, le=20, description="Max documents to return")

    @root_validator(pre=True)
    def _ensure_question(cls, values):
        question = values.get("question") or values.get("query")
        if not question:
            raise ValueError("question is required")
        values["question"] = question
        return values


def _neo4j_driver():
    auth = (NEO4J_USER, NEO4J_PASSWORD) if NEO4J_USER and NEO4J_PASSWORD else None
    return GraphDatabase.driver(NEO4J_URL, auth=auth)


def index_news_articles(items: List[Dict[str, Any]]) -> None:
    get_store().upsert_items(NEWS_COLLECTION, items)


def initialize_vector_store() -> None:
    get_store().ensure_runtime_collections()


def index_gdelt_events(items: List[Dict[str, Any]]) -> None:
    get_store().upsert_items(GDELT_EVENT_COLLECTION, items)


def delete_news_articles(article_ids: List[str]) -> None:
    ids = [str(article_id).strip() for article_id in article_ids if str(article_id).strip()]
    if ids:
        get_store().delete_original_ids(NEWS_COLLECTION, ids)


def _search_gdelt_events(question: str, limit: int, offset: int = 0) -> tuple[int, List[Dict[str, Any]]]:
    store = get_store()
    count = store.count(GDELT_EVENT_COLLECTION)
    if count <= 0:
        return 0, []
    requested = offset + limit
    candidates = []
    for point in store.query(GDELT_EVENT_COLLECTION, question, min(requested, count)):
        meta = point.get("payload") or {}
        title = meta.get("title") or "GDELT event"
        tickers = resolve_company_tickers(
            f"{title} {meta.get('actor1') or ''} {meta.get('actor2') or ''}"
        )
        candidates.append({
            "event_id": meta.get("gdelt_event_id"),
            "title": title,
            "summary": meta.get("document") or "",
            "url": meta.get("url") or "",
            "source": meta.get("source") or "GDELT",
            "published_ts": meta.get("published_ts"),
            "event_code": meta.get("event_code"),
            "root_event_code": meta.get("root_event_code"),
            "tone": meta.get("average_tone"),
            "goldstein_scale": meta.get("goldstein_scale"),
            "location": meta.get("location"),
            "country_code": meta.get("country_code"),
            "actor1": meta.get("actor1"),
            "actor2": meta.get("actor2"),
            "tickers": tickers,
            "title_inferred": bool(meta.get("title_inferred_from_url")),
            "score": 1.0 - float(point.get("score") or 0.0),
        })
    candidates.sort(key=lambda hit: hit["score"] if hit["score"] is not None else float("inf"))
    hits = candidates[offset:requested]
    for index, hit in enumerate(hits, start=offset + 1):
        hit["rank"] = index
    return count, hits


def _project_vector(arr: np.ndarray, dim: int) -> np.ndarray:
    """
    Deterministic random projection to keep output dim stable regardless of embedder size.
    """
    if arr.size == 0:
        return np.zeros(dim, dtype=np.float32)
    rng = np.random.default_rng(EMBED_PROJ_SEED)
    proj = rng.standard_normal((dim, arr.size), dtype=np.float32)
    return (proj @ arr).astype(np.float32)


def _query_vectors(question: str, tickers: List[str], top_k: int) -> List[Dict[str, Any]]:
    """
    Retrieve semantically similar articles. If tickers are provided, prefer those rows.
    """
    try:
        points = get_store().query(NEWS_COLLECTION, question, min(top_k * 3, 30))
    except Exception as exc:
        logging.error("Qdrant news query failed: %s", exc)
        return []
    docs = []
    for point in points:
        meta = point.get("payload") or {}
        docs.append({
            "title": meta.get("title") or "",
            "url": meta.get("url") or "",
            "tickers": meta.get("tickers") or [],
            "published_ts": meta.get("published_ts"),
            "score": 1.0 - float(point.get("score") or 0.0),
        })

    if tickers:
        normalized = {t.upper() for t in tickers}
        prioritized = [d for d in docs if normalized.intersection({t.upper() for t in d.get("tickers", [])})]
        if prioritized:
            docs = prioritized

    if RAG_RECENCY_DAYS > 0:
        cutoff = time.time() - RAG_RECENCY_DAYS * 86400
        recent_docs = [d for d in docs if d.get("published_ts") and d["published_ts"] >= cutoff]
        if recent_docs:
            docs = recent_docs

    return docs[:top_k]


def _query_graph(tickers: List[str]) -> List[Dict[str, Any]]:
    if not tickers:
        return []
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            relations = []
            for t in tickers:
                cypher = """
                MATCH (sec:Security {symbol: $sym})<-[:MENTIONS_SECURITY]-(art:Article)
                OPTIONAL MATCH (company:Company)-[:ISSUES]->(sec)
                RETURN sec.symbol AS symbol,
                       art.title AS title,
                       art.url AS url,
                       collect(DISTINCT company.name) AS entities
                ORDER BY art.published_ts DESC
                LIMIT 25
                """
                res = session.run(cypher, sym=t.upper())
                for row in res:
                    relations.append(
                        {
                            "ticker": row["symbol"],
                            "article_title": row["title"],
                            "article_url": row["url"],
                            "entities": row["entities"] or [],
                        }
                    )
            return relations
    except Exception as exc:
        logging.error("Neo4j query failed: %s", exc)
        return []


def _embed_text(text: str, dim: int = EMBEDDING_DIM_OUT) -> List[float]:
    try:
        vec = embed_texts([text])[0]
        arr = np.array(vec, dtype=np.float32).flatten()
        arr = _project_vector(arr, dim)
        if NORMALIZE_EMBEDDING and np.linalg.norm(arr) > 0:
            arr = arr / np.linalg.norm(arr)
        return arr[:dim].tolist()
    except Exception as exc:
        logging.warning("Embedding failed: %s", exc)
        return []


def _summarize(question: str, docs: List[Dict[str, Any]], relations: List[Dict[str, Any]]) -> str:
    """
    Lightweight, deterministic summary to keep the endpoint usable without an LLM.
    """
    lines = [f"Question: {question}"]
    if docs:
        lines.append("Top articles:")
        for d in docs:
            title = d.get("title") or "Untitled"
            url = d.get("url") or ""
            tickers = ",".join(d.get("tickers") or [])
            lines.append(f"- {title} [{tickers}] {url}")
    if relations:
        lines.append("Graph relations:")
        for r in relations[:10]:
            if {"ticker", "article_title", "entities"} <= set(r.keys()):
                lines.append(
                    f"- {r.get('ticker')}: {r.get('article_title')} (entities: {', '.join(r.get('entities') or [])}) {r.get('article_url') or ''}"
                )
            else:
                lines.append(f"- {r.get('source')} -[{r.get('relation')}]-> {r.get('target')}")
    return "\n".join(lines)


def _synthesize_answer(question: str, docs: List[Dict[str, Any]], relations: List[Dict[str, Any]]) -> str:
    """
    Try to generate an LLM answer if configured; otherwise fall back to deterministic summary.
    """
    if RAG_LLM_PROVIDER not in ("openai", "openai-compatible", "ollama"):
        return _summarize(question, docs, relations)
    # Provide a sensible default for Ollama if base URL is not configured
    base = LLM_API_BASE
    if not base and RAG_LLM_PROVIDER == "ollama":
        base = DEFAULT_OLLAMA_BASE

    try:
        import openai
    except Exception as exc:
        logging.warning("OpenAI client not available (%s); using summary.", exc)
        return _summarize(question, docs, relations)

    client_kwargs = {}
    if base:
        client_kwargs["base_url"] = base
    # Some local OpenAI-compatible servers accept empty api_key; pass None if not set.
    client = openai.OpenAI(api_key=LLM_API_KEY or None, **client_kwargs)

    context_lines = []
    for d in docs:
        title = d.get("title") or "Untitled"
        url = d.get("url") or ""
        tickers = ",".join(d.get("tickers") or [])
        context_lines.append(f"- {title} [{tickers}] {url}")
    for r in relations[:10]:
        context_lines.append(f"- REL: {r.get('source')} -[{r.get('relation')}]-> {r.get('target')}")

    prompt = (
        "You are a financial assistant. Answer the question using ONLY the provided context. "
        "If you are unsure, say you don't have enough information. Provide 2-4 sentences.\n\n"
        f"Question: {question}\n"
        "Context:\n" + "\n".join(context_lines)
    )

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        logging.warning("OpenAI call failed (%s); using summary.", exc)
        return _summarize(question, docs, relations)


def _rag_signal(question: str, docs: List[Dict[str, Any]], relations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Lightweight numeric features derived from retrieval/graph for downstream consumers."""
    hit_count = len(docs)
    graph_count = len(relations)
    avg_score = None
    scores = [d.get("score") for d in docs if isinstance(d.get("score"), (int, float))]
    if scores:
        avg_score = sum(scores) / len(scores)
    embedding = _embed_text(question)
    recency = None
    ts_vals = []
    for d in docs:
        meta_ts = None
        meta = d if isinstance(d, dict) else {}
        # metadatas may carry ts inside; fall back to None
        meta_ts = meta.get("published_ts")
        if meta_ts:
            ts_vals.append(meta_ts)
    if ts_vals:
        recency = max(ts_vals)
    confidence = min(1.0, 0.2 * hit_count + 0.1 * graph_count + (0.1 if recency else 0))
    return {
        "hit_count": hit_count,
        "graph_count": graph_count,
        "avg_score": avg_score,
        "embedding": embedding,
        "recency_ts": recency,
        "confidence": confidence,
    }

# simple in-memory cache for RAG responses keyed by (question,tickers,top_k)
_RAG_CACHE: Dict[str, Dict[str, Any]] = {}

def _cache_key(question: str, tickers: List[str], top_k: int) -> str:
    return f"{question}|{','.join(sorted(tickers))}|{top_k}"


@router.post("/")
async def rag_query(body: RagQuery):
    """
    Retrieve relevant news snippets and optional graph relations for the given question/tickers.
    """
    key = _cache_key(body.question, body.tickers, body.top_k)
    cached = _RAG_CACHE.get(key)
    now_ts = datetime.now(timezone.utc).timestamp()
    if cached and (now_ts - cached["ts"]) <= RAG_CACHE_TTL:
        return cached["payload"]

    docs = _query_vectors(body.question, body.tickers, body.top_k)
    relations = _query_graph(body.tickers)
    summary = _synthesize_answer(body.question, docs, relations)
    signal = _rag_signal(body.question, docs, relations)

    payload = {
        "question": body.question,
        "tickers": body.tickers,
        "hits": docs,
        "graph_relations": relations,
        "answer": summary,
        "signal": signal,
        "notes": "Results are retrieval-only; add LLM re-ranking/synthesis next.",
    }
    _RAG_CACHE[key] = {"ts": now_ts, "payload": payload}
    return payload


@router.get("/health")
async def rag_health():
    status = {"neo4j": False, "qdrant": get_store().healthy()}
    # Check neo4j
    try:
        driver = _neo4j_driver()
        driver.verify_connectivity()
        status["neo4j"] = True
    except Exception as exc:
        logging.error("Neo4j health check failed: %s", exc)
    return status


@router.get("/events/search")
async def search_gdelt_events(
    q: str = Query(..., min_length=2, max_length=300),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=900),
):
    try:
        count, hits = _search_gdelt_events(q.strip(), limit, offset)
    except Exception as exc:
        logging.exception("GDELT event search failed")
        raise HTTPException(status_code=503, detail="Historical event search is unavailable") from exc
    return {
        "query": q.strip(),
        "collection_count": count,
        "offset": offset,
        "limit": limit,
        "returned": len(hits),
        "has_more": offset + len(hits) < count and len(hits) == limit,
        "hits": hits,
    }


@router.get("/events/{event_id}/related")
async def related_gdelt_articles(
    event_id: str,
    limit: int = Query(20, ge=1, le=100),
):
    if not event_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid GDELT event ID")
    try:
        articles = persistent_store.list_gdelt_mentions(event_id, limit)
    except Exception as exc:
        logging.exception("GDELT related article lookup failed")
        raise HTTPException(status_code=503, detail="Related event coverage is unavailable") from exc
    return {"event_id": event_id, "count": len(articles), "articles": articles}


@router.get("/companies/{symbol}/articles")
async def company_articles(
    symbol: str,
    limit: int = Query(50, ge=1, le=200),
):
    normalized = symbol.strip().upper()
    if not normalized or len(normalized) > 10:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    try:
        with _neo4j_driver() as driver:
            result = driver.execute_query(
                """
                MATCH (company:Company)-[:ISSUES]->(security:Security {symbol: $symbol})
                OPTIONAL MATCH (article:Article)-[:MENTIONS_SECURITY]->(security)
                RETURN company.name AS company, security.symbol AS symbol,
                       security.listing_name AS security_name,
                       article.url AS url, article.title AS title, article.summary AS summary,
                       article.published_ts AS published_ts, article.source AS source,
                       article.provider AS provider
                ORDER BY article.published_ts DESC
                LIMIT $limit
                """,
                symbol=normalized,
                limit=limit,
            )
        rows = [dict(record) for record in result.records]
    except Exception as exc:
        logging.exception("Company article graph lookup failed")
        raise HTTPException(status_code=503, detail="Company article graph is unavailable") from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Security not found")
    articles = [
        {key: row.get(key) for key in ("url", "title", "summary", "published_ts", "source", "provider")}
        for row in rows
        if row.get("url")
    ]
    first = rows[0]
    return {
        "company": first.get("company"),
        "symbol": normalized,
        "security_name": first.get("security_name"),
        "article_count": len(articles),
        "articles": articles,
    }
