import os
import re

import requests


SERVICE_URL = os.getenv("SENTIMENT_SERVICE_URL", "http://sentiment-model:8091").rstrip("/")
HF_API_URL = "https://huggingface.co/api/models"
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
CURATED_MODELS = {
    "ProsusAI/finbert": {
        "training": "Financial PhraseBank financial news sentences",
        "limitation": "Measures English financial tone; it does not predict price direction.",
        "label_map": {"positive": "positive", "negative": "negative", "neutral": "neutral"},
    },
    "yiyanghkust/finbert-tone": {
        "training": "10,000 manually labeled analyst-report sentences",
        "limitation": "Analyst-report tone may not transfer cleanly to breaking news.",
        "label_map": {"label_0": "neutral", "label_1": "positive", "label_2": "negative"},
    },
}


def _request(method: str, path: str, *, timeout: float = 15, **kwargs):
    response = requests.request(method, f"{SERVICE_URL}{path}", timeout=timeout, **kwargs)
    if not response.ok:
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text
        raise RuntimeError(str(detail or f"Sentiment service returned {response.status_code}")[:1000])
    return response.json()


def status() -> dict:
    try:
        return {"available": True, **_request("GET", "/health", timeout=5)}
    except Exception as exc:
        return {"available": False, "loaded": False, "error": str(exc)[:500]}


def load(model: str, label_map: dict[str, str] | None = None) -> dict:
    model_id = str(model or "").strip()
    if not MODEL_RE.fullmatch(model_id):
        raise ValueError("Use a Hugging Face owner/model repository ID")
    mapping = label_map or CURATED_MODELS.get(model_id, {}).get("label_map") or {}
    return _request("POST", "/load", timeout=1800, json={"model": model_id, "label_map": mapping})


def unload() -> dict:
    return _request("POST", "/unload", timeout=30)


def predict(texts: list[str]) -> dict:
    return _request("POST", "/predict", timeout=60, json={"texts": texts})


def catalog(query: str = "") -> list[dict]:
    search = str(query or "financial sentiment").strip()[:100]
    response = requests.get(
        HF_API_URL,
        params={"search": search, "pipeline_tag": "text-classification", "sort": "downloads", "direction": -1, "limit": 12, "full": "true"},
        headers={"Authorization": f"Bearer {os.getenv('HF_TOKEN')}"} if os.getenv("HF_TOKEN") else {},
        timeout=20,
    )
    response.raise_for_status()
    rows = []
    for model in response.json():
        model_id = model.get("id") or model.get("modelId")
        if not model_id or not MODEL_RE.fullmatch(model_id):
            continue
        curated = CURATED_MODELS.get(model_id, {})
        siblings = model.get("siblings") or []
        has_safetensors = any(str(item.get("rfilename") or "").endswith(".safetensors") for item in siblings)
        rows.append({
            "id": model_id,
            "downloads": int(model.get("downloads") or 0),
            "likes": int(model.get("likes") or 0),
            "last_modified": model.get("lastModified"),
            "tags": model.get("tags") or [],
            "library": model.get("library_name"),
            "private": bool(model.get("private")),
            "gated": bool(model.get("gated")),
            "has_safetensors": has_safetensors,
            "load_allowed": has_safetensors or model_id in CURATED_MODELS,
            "training": curated.get("training"),
            "limitation": curated.get("limitation"),
            "url": f"https://huggingface.co/{model_id}",
        })
    return rows
