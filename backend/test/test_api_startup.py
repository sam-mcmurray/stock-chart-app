import asyncio
from threading import Event
from unittest.mock import MagicMock

import pytest

import main


@pytest.mark.parametrize("failure", [None, "schema", "qdrant"])
def test_startup_timings_preserve_readiness_gates(monkeypatch, caplog, failure):
    caplog.set_level("INFO", logger="main")
    calls = []

    async def schema():
        calls.append("schema")
        if failure == "schema":
            raise RuntimeError("schema unavailable")

    def vector_store():
        calls.append("qdrant")
        if failure == "qdrant":
            raise RuntimeError("qdrant unavailable")

    dispatch = MagicMock(side_effect=lambda *args: calls.append("consumer"))
    monkeypatch.setattr(main, "_ensure_durable_schema", schema)
    monkeypatch.setattr(main.rag_router, "initialize_vector_store", vector_store)
    for name in ("_gdelt_event_stop", "_news_index_stop"):
        monkeypatch.setattr(main, name, Event())
    for name in ("_gdelt_event_task", "_news_index_task"):
        monkeypatch.setattr(main, name, None)
    monkeypatch.setenv("GDELT_EVENT_QUEUE_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("NEWS_INDEX_QUEUE_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("NEWS_CLEANUP_QUEUE_CONSUMER_ENABLED", "false")
    monkeypatch.setenv("RUN_API_EMBEDDED_WORKERS", "false")

    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", dispatch)
        await main.start_workers()

    if failure:
        with pytest.raises(RuntimeError, match=f"{failure} unavailable"):
            asyncio.run(run())
        dispatch.assert_not_called()
        assert "Qdrant collections ready" not in caplog.text
        if failure == "schema":
            assert calls == ["schema"]
            assert "database schema ready" not in caplog.text
    else:
        asyncio.run(run())
        assert calls == ["schema", "qdrant", "consumer", "consumer"]
        assert "database schema ready in" in caplog.text
        assert "Qdrant collections ready in" in caplog.text
