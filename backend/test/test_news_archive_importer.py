import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


def _module(**attrs):
    mod = ModuleType("stub")
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _load_importer(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.news_indexer", _module(NewsIndexer=MagicMock()))
    monkeypatch.setitem(
        sys.modules,
        "workers.news_worker",
        _module(
            EMBEDDER_MODEL="test-model",
            VSTORE_PATH="/tmp/chroma",
            NewsWorker=MagicMock(),
            _article_exists=MagicMock(return_value=False),
            _normalize_article=lambda article, source: {
                "url": article.get("url"),
                "title": article.get("headline") or "",
                "summary": article.get("summary") or "",
                "published_ts": article.get("datetime"),
                "source": article.get("source") or source,
                "tickers": [],
            },
        ),
    )
    path = os.path.join(os.path.dirname(__file__), "..", "workers", "news_archive_importer.py")
    spec = importlib.util.spec_from_file_location("workers.news_archive_importer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_iter_archive_rows_reads_json_and_jsonl(tmp_path, monkeypatch):
    importer = _load_importer(monkeypatch)
    (tmp_path / "a.json").write_text(json.dumps({"articles": [{"url": "https://a.example", "title": "A"}]}), encoding="utf-8")
    (tmp_path / "b.jsonl").write_text('{"url":"https://b.example","title":"B"}\n', encoding="utf-8")

    rows = list(importer.iter_archive_rows(tmp_path))

    assert [row["url"] for row in rows] == ["https://a.example", "https://b.example"]


def test_normalize_archive_article_maps_gdelt_fields(monkeypatch):
    importer = _load_importer(monkeypatch)

    item = importer.normalize_archive_article(
        {
            "url": "https://example.com/story",
            "title": "Market story",
            "snippet": "Summary",
            "domain": "example.com",
            "seendate": "20260605T120000Z",
        }
    )

    assert item["url"] == "https://example.com/story"
    assert item["title"] == "Market story"
    assert item["summary"] == "Summary"
    assert item["source"] == "example.com"
    assert item["published_ts"] > 0


def test_import_archive_batches_to_indexer_and_recent_store(tmp_path, monkeypatch):
    importer = _load_importer(monkeypatch)
    rows = [
        {"url": "https://example.com/1", "title": "One"},
        {"url": "https://example.com/2", "title": "Two"},
    ]
    archive = Path(tmp_path / "archive.jsonl")
    archive.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    indexer = MagicMock()
    worker = MagicMock()

    stats = importer.import_archive(archive, batch_size=1, indexer=indexer, worker=worker)

    assert stats == {"seen": 2, "imported": 2, "skipped": 0}
    assert indexer.upsert_batch.call_count == 2
    assert worker._add_recent.call_count == 2
