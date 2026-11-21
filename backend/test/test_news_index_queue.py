import json
from threading import Event
from unittest.mock import MagicMock

from services import news_index_queue


def test_enqueue_places_durable_payloads_on_pending_queue():
    client = MagicMock()
    items = [{"id": "article-1", "title": "Company reports earnings"}]

    assert news_index_queue.enqueue(items, client=client) == 1

    client.lpush.assert_called_once_with(
        news_index_queue.PENDING_KEY,
        news_index_queue._encode(items[0]),
    )


def test_consumer_batches_and_acknowledges_articles(monkeypatch):
    client = MagicMock()
    payload = news_index_queue._encode({"id": "article-1"})
    client.rpoplpush.side_effect = [None, payload, None]
    stop = Event()

    def index_batch(items):
        assert items == [{"id": "article-1"}]
        stop.set()

    news_index_queue.consume_forever(index_batch, stop, client=client)

    client.pipeline.return_value.lrem.assert_called_once_with(
        news_index_queue.PROCESSING_KEY,
        1,
        payload,
    )
