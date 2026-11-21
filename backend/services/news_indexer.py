import hashlib, os, time, json
from datetime import datetime, timezone, timedelta
from typing import Iterable, Dict, Any, List

import chromadb
from chromadb.utils import embedding_functions

class NewsIndexer:
    """
    Small wrapper over ChromaDB collection to index news for RAG.
    Each doc id = sha1(url) or provided id.
    """
    def __init__(self, path: str, collection: str = "news", model: str = "all-MiniLM-L6-v2"):
        os.makedirs(path, exist_ok=True)
        self.client = chromadb.PersistentClient(path=path)
        ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model)
        self.col = self.client.get_or_create_collection(name=collection, embedding_function=ef)

    @staticmethod
    def _doc_id(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def upsert_batch(self, items: Iterable[Dict[str, Any]], *, preserve_existing: bool = False):
        """
        items: {url, title, text, published_ts, tickers:[...], source}
        """
        docs, ids, metas = [], [], []
        for it in items:
            url   = it.get("url") or ""
            title = it.get("title") or ""
            text  = it.get("text") or it.get("summary") or title
            ts    = int(it.get("published_ts") or time.time())
            toks  = it.get("tickers") or []
            src   = it.get("source") or "unknown"
            provider = it.get("provider") or ""
            if not url or not text:
                continue
            ids.append(str(it.get("id") or self._doc_id(url)))
            docs.append(text)
            metadata = {"url": url, "title": title, "published_ts": ts, "source": src}
            if provider:
                metadata["provider"] = provider
            for key, value in (it.get("metadata") or {}).items():
                if value is not None and isinstance(value, (str, int, float, bool)):
                    metadata[key] = value
            if toks:
                metadata["tickers"] = toks
            metas.append(metadata)
        if ids and preserve_existing:
            existing = set(self.col.get(ids=ids, include=[]).get("ids") or [])
            keep = [index for index, item_id in enumerate(ids) if item_id not in existing]
            ids = [ids[index] for index in keep]
            docs = [docs[index] for index in keep]
            metas = [metas[index] for index in keep]
        if ids:
            self.col.upsert(ids=ids, documents=docs, metadatas=metas)

    def query_similar(self, text: str, k: int = 5):
        return self.col.query(query_texts=[text], n_results=k)
