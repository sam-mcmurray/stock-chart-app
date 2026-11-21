import ipaddress
import json
import logging
import os
import socket
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from services.decision_engine import classify_catalysts, news_sentiment

logger = logging.getLogger(__name__)

ENABLED = os.getenv("CANDIDATE_ARTICLE_ANALYSIS_ENABLED", "true").lower() == "true"
FETCH_LIMIT = int(os.getenv("CANDIDATE_ARTICLE_FETCH_LIMIT", "3"))
FETCH_TIMEOUT = float(os.getenv("CANDIDATE_ARTICLE_FETCH_TIMEOUT_SECONDS", "6"))
MAX_TEXT_CHARS = int(os.getenv("CANDIDATE_ARTICLE_MAX_TEXT_CHARS", "12000"))
LLM_TIMEOUT = float(os.getenv("CANDIDATE_LLM_TIMEOUT_SECONDS", "15"))
_TEXT_CACHE: dict[str, str] = {}


def should_analyze_candidate(decision: dict) -> bool:
    return ENABLED and decision.get("action") == "long_candidate"


def _public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        return all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (OSError, ValueError):
        return False


def extract_article_text(url: str) -> str:
    if url in _TEXT_CACHE:
        return _TEXT_CACHE[url]
    if not _public_http_url(url):
        return ""
    try:
        response = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": "StockChartResearch/1.0"},
            allow_redirects=False,
        )
        response.raise_for_status()
        if "html" not in response.headers.get("content-type", "").lower():
            return ""
        soup = BeautifulSoup(response.text[:1_000_000], "html.parser")
        for node in soup(["script", "style", "nav", "footer", "aside"]):
            node.decompose()
        paragraphs = [node.get_text(" ", strip=True) for node in soup.find_all("p")]
        text = "\n".join(part for part in paragraphs if len(part) >= 40)[:MAX_TEXT_CHARS]
    except Exception as exc:
        logger.info("Article text unavailable for %s: %s", url, exc)
        text = ""
    _TEXT_CACHE[url] = text
    return text


def _article_context(articles: list[dict]) -> list[dict]:
    context = []
    for article in articles[:max(1, FETCH_LIMIT)]:
        body = article.get("text") or ""
        if len(body) < 500 and article.get("url"):
            body = extract_article_text(article["url"]) or body
        context.append({
            "title": article.get("title") or article.get("headline") or "",
            "summary": article.get("summary") or "",
            "url": article.get("url") or "",
            "text": body[:MAX_TEXT_CHARS],
        })
    return context


def _llm_analysis(symbol: str, context: list[dict]) -> dict | None:
    from routers import rag_api

    if rag_api.RAG_LLM_PROVIDER not in {"openai", "openai-compatible", "ollama"}:
        return None
    base = rag_api.LLM_API_BASE or (rag_api.DEFAULT_OLLAMA_BASE if rag_api.RAG_LLM_PROVIDER == "ollama" else "https://api.openai.com/v1")
    headers = {"Content-Type": "application/json"}
    if rag_api.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {rag_api.LLM_API_KEY}"
    prompt = (
        f"Analyze these articles for a possible long trade in {symbol}. Return JSON only with: "
        "sentiment (positive|neutral|negative), score (-1 to 1), catalyst, catalyst_direction "
        "(positive|neutral|negative), confidence (0 to 1), blocking_risks (array), evidence (array of short claims). "
        "Use only supplied article text. Treat offerings, dilution, bankruptcy, fraud, regulatory rejection, and severe legal risk as blocking.\n\n"
        + json.dumps(context)[:30000]
    )
    try:
        response = requests.post(
            f"{base.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": rag_api.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=LLM_TIMEOUT,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        if result.get("sentiment") not in {"positive", "neutral", "negative"}:
            return None
        result["source"] = "llm"
        return result
    except Exception as exc:
        logger.warning("Candidate LLM analysis unavailable: %s", exc)
        return None


def analyze_candidate_articles(symbol: str, articles: list[dict]) -> dict:
    context = _article_context(articles)
    llm_result = _llm_analysis(symbol, context)
    if llm_result:
        llm_result["article_count"] = len(context)
        return llm_result
    sentiment = news_sentiment(context)
    catalyst = classify_catalysts(context)
    return {
        "source": "rules",
        "sentiment": sentiment["label"],
        "score": sentiment["score"],
        "catalyst": catalyst["primary"],
        "catalyst_direction": catalyst["direction"],
        "confidence": min(0.75, 0.25 + 0.1 * len(context)),
        "blocking_risks": [match["type"] for match in catalyst["matches"] if match["direction"] == "negative"],
        "evidence": (sentiment["positive_hits"] + sentiment["negative_hits"])[:8],
        "article_count": len(context),
    }


def apply_analysis_guardrail(decision: dict, analysis: dict) -> dict:
    decision["financial_analysis"] = analysis
    blocking = list(analysis.get("blocking_risks") or [])
    if analysis.get("sentiment") == "negative" or analysis.get("catalyst_direction") == "negative" or blocking:
        decision["action"] = "avoid"
        reason = "llm_negative_financial_analysis" if analysis.get("source") == "llm" else "article_negative_financial_analysis"
        if reason not in decision.setdefault("reasons", []):
            decision["reasons"].append(reason)
    return decision
