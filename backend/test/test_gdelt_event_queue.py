import json
from threading import Event
from unittest.mock import MagicMock

from services import gdelt_event_queue


def test_enqueue_serializes_events_to_pending_list():
    client = MagicMock()
    items = [{"id": "gdelt:1", "title": "Event"}]

    assert gdelt_event_queue.enqueue(items, client=client) == 1

    client.lpush.assert_called_once_with(
        gdelt_event_queue.PENDING_KEY,
        gdelt_event_queue._encode_payload(items[0]),
    )


def test_consumer_indexes_and_acknowledges_claimed_batch(monkeypatch):
    client = MagicMock()
    payload = json.dumps({"id": "gdelt:1", "title": "Event"})
    client.rpoplpush.side_effect = [None, payload, None]
    stop = Event()

    def index_batch(items):
        assert items == [{"id": "gdelt:1", "title": "Event"}]
        stop.set()

    monkeypatch.setattr(gdelt_event_queue, "MAX_BATCH_SIZE", 2)
    gdelt_event_queue.consume_forever(index_batch, stop, client=client)

    client.pipeline.return_value.lrem.assert_called_once_with(
        gdelt_event_queue.PROCESSING_KEY, 1, payload
    )


def test_consumer_isolates_poison_record_and_acks_good_record(monkeypatch):
    client = MagicMock()
    poison = gdelt_event_queue._encode_payload({"id": "poison"})
    good = gdelt_event_queue._encode_payload({"id": "good"})
    client.rpoplpush.side_effect = [None, poison, good, None]
    stop = Event()

    def index_batch(items):
        if len(items) > 1 or items[0]["id"] == "poison":
            raise ValueError("invalid record")
        stop.set()

    monkeypatch.setattr(gdelt_event_queue, "MAX_BATCH_SIZE", 3)
    monkeypatch.setattr(gdelt_event_queue, "MAX_RETRIES", 1)
    monkeypatch.setattr(gdelt_event_queue, "MIN_DEAD_LETTER_AGE_SECONDS", 0)
    gdelt_event_queue.consume_forever(index_batch, stop, client=client)

    calls = client.pipeline.return_value.method_calls
    assert any(call.args[0] == gdelt_event_queue.DEAD_LETTER_KEY for call in calls if call[0] == "lpush")
    assert any(call.args == (gdelt_event_queue.PROCESSING_KEY, 1, good) for call in calls if call[0] == "lrem")


def test_replay_dead_letters_moves_records_atomically():
    client = MagicMock()
    payload = gdelt_event_queue._encode_payload({"id": "gdelt:1"}, attempts=5)
    client.lindex.side_effect = [payload, None]
    client.rpoplpush.return_value = payload

    assert gdelt_event_queue.replay_dead_letters(limit=2, client=client) == 1

    client.rpoplpush.assert_any_call(
        gdelt_event_queue.DEAD_LETTER_KEY,
        gdelt_event_queue.PENDING_KEY,
    )


def test_replay_dead_letters_leaves_malformed_payload_quarantined():
    client = MagicMock()
    client.lindex.return_value = '{"queue_version":1,"raw_payload":"bad"}'

    assert gdelt_event_queue.replay_dead_letters(client=client) == 0

    client.rpoplpush.assert_not_called()
