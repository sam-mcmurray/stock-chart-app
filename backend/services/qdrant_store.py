from __future__ import annotations

import hashlib
import os
import time
import uuid
from functools import lru_cache
from threading import Lock
from typing import Any, Iterable, TYPE_CHECKING

import requests
if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_VECTOR_SIZE = max(1, int(os.getenv("QDRANT_VECTOR_SIZE", "384")))
QDRANT_REQUEST_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("QDRANT_REQUEST_TIMEOUT_SECONDS", "60"))
)
EMBEDDER_MODEL = os.getenv("EMBEDDER_MODEL", "all-MiniLM-L6-v2")
NEWS_COLLECTION = os.getenv("QDRANT_NEWS_COLLECTION", "news")
GDELT_EVENT_COLLECTION = os.getenv("QDRANT_GDELT_EVENT_COLLECTION", "gdelt_events")

_ID_NAMESPACE = uuid.UUID("9e8939ad-72e6-4868-a79d-a5370aa771f3")
_model_lock = Lock()


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY
    return headers


def point_id(original_id: Any) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, str(original_id)))


def document_id(item: dict[str, Any]) -> str:
    explicit = str(item.get("id") or "").strip()
    if explicit:
        return explicit
    url = str(item.get("url") or "").strip()
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDER_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    with _model_lock:
        vectors = _model().encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    return [vector.astype("float32").tolist() for vector in vectors]


class QdrantStore:
    def __init__(self, base_url: str = QDRANT_URL):
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, **kwargs):
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=_headers(),
            timeout=QDRANT_REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            return response.json()
        return {"text": response.text}

    def wait_ready(self, attempts: int = 30, delay_seconds: float = 2.0) -> None:
        error = None
        for _ in range(max(1, attempts)):
            try:
                self._request("GET", "/readyz")
                return
            except Exception as exc:
                error = exc
                time.sleep(max(0.1, delay_seconds))
        raise RuntimeError(f"Qdrant did not become ready: {error}")

    def healthy(self) -> bool:
        try:
            self._request("GET", "/readyz")
            return True
        except Exception:
            return False

    def ensure_collection(self, name: str) -> None:
        try:
            self._request("GET", f"/collections/{name}")
            return
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
        try:
            self._request(
                "PUT",
                f"/collections/{name}",
                json={"vectors": {"size": QDRANT_VECTOR_SIZE, "distance": "Cosine"}},
            )
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 409:
                raise

    def ensure_runtime_collections(self) -> None:
        self.wait_ready()
        self.ensure_collection(NEWS_COLLECTION)
        self.ensure_collection(GDELT_EVENT_COLLECTION)

    @staticmethod
    def _payload(item: dict[str, Any], original_id: str, document: str) -> dict[str, Any]:
        payload = {
            "original_id": original_id,
            "url": str(item.get("url") or ""),
            "title": str(item.get("title") or ""),
            "document": document,
            "published_ts": int(item.get("published_ts") or time.time()),
            "source": str(item.get("source") or "unknown"),
            "provider": str(item.get("provider") or ""),
            "tickers": item.get("tickers") or [],
        }
        for key, value in (item.get("metadata") or {}).items():
            if value is not None and isinstance(value, (str, int, float, bool, list)):
                payload[key] = value
        return payload

    def upsert_items(
        self,
        collection: str,
        items: Iterable[dict[str, Any]],
        *,
        update_mode: str = "upsert",
    ) -> int:
        prepared = []
        texts = []
        for item in items:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            document = str(item.get("text") or item.get("summary") or title).strip()
            if not url or not document:
                continue
            original_id = document_id(item)
            prepared.append((item, original_id, document))
            texts.append(document)
        vectors = embed_texts(texts)
        points = [
            {
                "id": point_id(original_id),
                "vector": vector,
                "payload": self._payload(item, original_id, document),
            }
            for (item, original_id, document), vector in zip(prepared, vectors)
        ]
        if points:
            self.upsert_points(collection, points, update_mode=update_mode)
        return len(points)

    def upsert_points(
        self,
        collection: str,
        points: list[dict[str, Any]],
        *,
        update_mode: str = "upsert",
    ) -> None:
        if not points:
            return
        self._request(
            "PUT",
            f"/collections/{collection}/points?wait=true",
            json={"points": points, "update_mode": update_mode},
        )

    def delete_original_ids(self, collection: str, original_ids: list[str]) -> None:
        ids = [point_id(value) for value in original_ids if str(value).strip()]
        if ids:
            self._request(
                "POST",
                f"/collections/{collection}/points/delete?wait=true",
                json={"points": ids},
            )

    def count(self, collection: str) -> int:
        payload = self._request("GET", f"/collections/{collection}")
        return int((payload.get("result") or {}).get("points_count") or 0)

    def scroll_payloads(
        self,
        collection: str,
        *,
        limit: int = 500,
        offset: str | int | None = None,
    ) -> tuple[list[dict[str, Any]], str | int | None]:
        body: dict[str, Any] = {
            "limit": max(1, min(int(limit), 1000)),
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        response = self._request(
            "POST",
            f"/collections/{collection}/points/scroll",
            json=body,
        )
        result = response.get("result") or {}
        return result.get("points") or [], result.get("next_page_offset")

    def query(self, collection: str, text: str, limit: int) -> list[dict[str, Any]]:
        vector = embed_texts([text])[0]
        payload = self._request(
            "POST",
            f"/collections/{collection}/points/query",
            json={
                "query": vector,
                "limit": max(1, int(limit)),
                "with_payload": True,
                "with_vector": False,
            },
        )
        result = payload.get("result") or {}
        return result.get("points") or []


@lru_cache(maxsize=1)
def get_store() -> QdrantStore:
    return QdrantStore()
