import gc
import json
import os
import re
import subprocess
import sys
from threading import Lock

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModelForSequenceClassification, AutoTokenizer


app = FastAPI(title="Financial Sentiment Evaluator")
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
LEGACY_TRUSTED_MODELS = {"ProsusAI/finbert", "yiyanghkust/finbert-tone"}
KNOWN_LABEL_MAPS = {
    "ProsusAI/finbert": {"positive": "positive", "negative": "negative", "neutral": "neutral"},
    "yiyanghkust/finbert-tone": {"label_0": "neutral", "label_1": "positive", "label_2": "negative"},
}
CUDA_PROBE_TIMEOUT_SECONDS = max(1, int(os.getenv("SENTIMENT_CUDA_PROBE_TIMEOUT_SECONDS", "15")))


def _probe_cuda(timeout_seconds: int = CUDA_PROBE_TIMEOUT_SECONDS) -> tuple[bool, str, str | None, list[str]]:
    probe = """
import json
import torch

if not torch.cuda.is_available():
    print(json.dumps({"ready": False, "error": "torch.cuda.is_available() returned false"}))
else:
    value = torch.ones(1, device="cuda:0").cpu().item()
    print(json.dumps({
        "ready": value == 1,
        "error": "",
        "gpu_name": torch.cuda.get_device_name(0),
        "cuda_arches": torch.cuda.get_arch_list(),
    }))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"CUDA validation exceeded {timeout_seconds}s; using CPU", None, []
    if result.returncode != 0:
        error = (result.stderr or result.stdout or f"CUDA probe exited {result.returncode}").strip()
        return False, error[-500:], None, []
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        return False, f"Invalid CUDA probe response: {exc}", None, []
    return (
        bool(payload.get("ready")),
        str(payload.get("error") or "")[:500],
        payload.get("gpu_name"),
        [str(item) for item in payload.get("cuda_arches") or []],
    )


REQUESTED_DEVICE = os.getenv("SENTIMENT_DEVICE", "cpu").strip().lower()
if REQUESTED_DEVICE == "cuda":
    CUDA_READY, CUDA_ERROR, CUDA_GPU_NAME, CUDA_ARCHES = _probe_cuda()
else:
    CUDA_READY = False
    CUDA_ERROR = "CUDA disabled for startup; set SENTIMENT_DEVICE=cuda when a GPU is available"
    CUDA_GPU_NAME = None
    CUDA_ARCHES = []
DEVICE = torch.device("cuda:0" if CUDA_READY else "cpu")
_lock = Lock()
_model = None
_tokenizer = None
_model_id = ""
_label_map: dict[str, str] = {}


class LoadRequest(BaseModel):
    model: str = Field(min_length=3, max_length=200)
    label_map: dict[str, str] = Field(default_factory=dict)


class PredictRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=32)


def _normalize_label_map(model_id: str, configured: dict[str, str], id2label: dict) -> dict[str, str]:
    source = configured or KNOWN_LABEL_MAPS.get(model_id) or {}
    normalized = {str(key).lower(): str(value).lower() for key, value in source.items()}
    allowed = {"positive", "neutral", "negative"}
    if normalized and set(normalized.values()) <= allowed:
        return normalized
    labels = {str(value).lower() for value in (id2label or {}).values()}
    if labels == allowed:
        return {label: label for label in labels}
    raise ValueError("The model uses generic labels. Provide an explicit positive/neutral/negative label map.")


def _unload() -> None:
    global _model, _tokenizer, _model_id, _label_map
    _model = None
    _tokenizer = None
    _model_id = ""
    _label_map = {}
    gc.collect()
    if CUDA_READY:
        torch.cuda.empty_cache()


@app.get("/health")
def health():
    memory = int(torch.cuda.memory_allocated()) if CUDA_READY else 0
    return {
        "ok": True,
        "cuda": CUDA_READY,
        "cuda_error": CUDA_ERROR or None,
        "requested_device": REQUESTED_DEVICE,
        "device": str(DEVICE),
        "gpu_name": CUDA_GPU_NAME,
        "cuda_arches": CUDA_ARCHES,
        "vram_allocated": memory,
        "model": _model_id or None,
        "loaded": _model is not None,
        "label_map": _label_map,
        "torch_version": torch.__version__,
    }


@app.post("/load")
def load_model(body: LoadRequest):
    global _model, _tokenizer, _model_id, _label_map
    model_id = body.model.strip()
    if not MODEL_RE.fullmatch(model_id):
        raise HTTPException(status_code=400, detail="Use a Hugging Face owner/model repository ID")
    with _lock:
        _unload()
        try:
            tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=False)
            model = AutoModelForSequenceClassification.from_pretrained(
                model_id,
                trust_remote_code=False,
                use_safetensors=None if model_id in LEGACY_TRUSTED_MODELS else True,
            )
            label_map = _normalize_label_map(model_id, body.label_map, model.config.id2label)
            model.eval().to(DEVICE)
        except Exception as exc:
            _unload()
            raise HTTPException(status_code=422, detail=str(exc)[:1000]) from exc
        _tokenizer, _model, _model_id, _label_map = tokenizer, model, model_id, label_map
    return health()


@app.post("/unload")
def unload_model():
    with _lock:
        _unload()
    return health()


@app.post("/predict")
def predict(body: PredictRequest):
    if _model is None or _tokenizer is None:
        raise HTTPException(status_code=409, detail="Load a sentiment model before previewing it")
    texts = [" ".join(str(text or "").split())[:12000] for text in body.texts]
    with _lock, torch.inference_mode():
        encoded = _tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
        encoded = {key: value.to(DEVICE) for key, value in encoded.items()}
        logits = _model(**encoded).logits
        probabilities = torch.softmax(logits, dim=-1).cpu().tolist()
    results = []
    for scores in probabilities:
        mapped = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
        for index, probability in enumerate(scores):
            raw_label = str(_model.config.id2label.get(index, f"LABEL_{index}")).lower()
            label = _label_map.get(raw_label)
            if label:
                mapped[label] += float(probability)
        label = max(mapped, key=mapped.get)
        results.append({
            "label": label,
            "confidence": mapped[label],
            "score": mapped["positive"] - mapped["negative"],
            "probabilities": mapped,
            "model": _model_id,
            "truncated_to_tokens": 512,
        })
    return {"results": results}
