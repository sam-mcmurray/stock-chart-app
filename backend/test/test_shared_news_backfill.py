from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from workers import news_backfill_worker, shared_news_backfill as shared


@contextmanager
def context(value):
    yield value


def worker():
    return SimpleNamespace(
        PIPELINE_VERSION="v1", CHECKPOINT_NAME="primary", PROCESS_RETRIES=False,
        YIELD_TO_LIVE_QUEUE=True,
        RETRY_EVERY_BATCHES=5, RETRY_BATCH_SIZE=4, BATCH_SIZE=8,
        LIVE_QUEUE_YIELD_SECONDS=5, MODEL_WAIT_SECONDS=60, r=object(),
        ensure_historical_article_index=Mock(), newest_historical_article_timestamp=Mock(),
        historical_article_partition_size=Mock(), list_historical_articles_newest=Mock(),
        _ForwardProgressRate=news_backfill_worker._ForwardProgressRate,
        configured=Mock(return_value=True), pending_counts=Mock(return_value={"total": 0}),
        persistent_store=SimpleNamespace(count_historical_article_retries=Mock(return_value=0)),
        _heartbeat=Mock(), process_batch=Mock(return_value={"completed_delta": 1, "failed": 0}),
    )


def test_live_and_model_priority_before_claim_then_save_before_ack(monkeypatch):
    w = worker()
    w.configured.side_effect = [False, True, True, True]
    w.pending_counts.side_effect = [{"total": 1}, {"total": 0}, {"total": 0}]
    saved = []
    w.process_batch.side_effect = lambda rows: saved.append(rows) or {"completed_delta": 1, "failed": 0}
    monkeypatch.setattr(shared.time, "sleep", Mock())
    for name in ("ensure_schema", "initialize", "fill"):
        monkeypatch.setattr(shared.queue, name, Mock())
    job = {"articles": [{"url": "https://test.invalid/recent", "published_ts": 100}]}
    claims = Mock(side_effect=lambda: context((object(), job) if not saved else None))
    monkeypatch.setattr(shared.queue, "claim", claims)
    monkeypatch.setattr(shared.queue, "snapshot", lambda _: {
        "source_exhausted": bool(saved), "shared_queue_pending": 0 if saved else 1})
    ack = Mock(side_effect=lambda *_: saved or pytest.fail("ack before persistence"))
    monkeypatch.setattr(shared.queue, "finish", ack)
    shared.run(w)
    assert w.process_batch.call_count == ack.call_count == 1
    assert claims.call_count == 2
    statuses = [call.args[0] for call in w._heartbeat.call_args_list]
    assert statuses == ["waiting_for_model", "waiting_for_live_queue",
                        "processing_historical_articles", "historical_articles_ok", "complete"]


def test_processing_exception_releases_claim_without_ack(monkeypatch):
    w = worker()
    w.process_batch.side_effect = RuntimeError("database interrupted")
    released = []

    @contextmanager
    def claimed():
        try:
            yield object(), {"articles": [{"url": "https://test.invalid/1"}]}
        finally:
            released.append(True)

    for name in ("ensure_schema", "initialize", "fill", "finish"):
        monkeypatch.setattr(shared.queue, name, Mock())
    monkeypatch.setattr(shared.queue, "claim", claimed)
    monkeypatch.setattr(shared.queue, "snapshot", lambda _: {})
    with pytest.raises(RuntimeError, match="interrupted"):
        shared.run(w)
    assert released == [True]
    shared.queue.finish.assert_not_called()


def test_dedicated_gpu_does_not_wait_for_other_gpu_live_queue(monkeypatch):
    w = worker()
    w.YIELD_TO_LIVE_QUEUE = False
    w.pending_counts.return_value = {"total": 52}
    for name in ("ensure_schema", "initialize", "fill"):
        monkeypatch.setattr(shared.queue, name, Mock())
    monkeypatch.setattr(shared.queue, "snapshot", lambda _: {})
    w.process_batch.side_effect = RuntimeError("reached inference")
    monkeypatch.setattr(shared.queue, "claim", lambda: context(
        (object(), {"articles": [{"url": "https://test.invalid/recent"}]})))
    with pytest.raises(RuntimeError, match="reached inference"):
        shared.run(w)
    w.process_batch.assert_called_once()
    assert all(c.args[0] != "waiting_for_live_queue" for c in w._heartbeat.call_args_list)
