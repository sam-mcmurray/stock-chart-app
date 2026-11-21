from workers.gdelt_mentions_importer import (
    MAX_QDRANT_SCROLL_PAGE_SIZE,
    _qdrant_event_ids,
    _load_state,
    _save_state,
    normalize_mention,
)


class FakeStore:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def scroll_payloads(self, collection, *, limit, offset=None):
        self.calls.append({"collection": collection, "limit": limit, "offset": offset})
        start = int(offset or 0)
        page = self.records[start:start + limit]
        next_offset = start + len(page) if start + len(page) < len(self.records) else None
        return page, next_offset


def test_mention_checkpoint_round_trip(tmp_path):
    path = tmp_path / "state.json"
    _save_state(path, {"20260623011500.mentions.CSV.zip"})
    assert _load_state(path) == {"20260623011500.mentions.CSV.zip"}


def test_normalize_mention_for_indexed_event():
    row = ["1307007199", "20260602000000", "20260602001500", "1", "example.com", "https://example.com/story", "4", "0", "5", "10", "1", "88", "500", "-1.25", "", ""]
    result = normalize_mention(row, {"1307007199"})
    assert result["event_id"] == "1307007199"
    assert result["confidence"] == 88.0
    assert result["document_tone"] == -1.25


def test_normalize_mention_skips_unindexed_event():
    row = ["1", "", "", "", "", "https://example.com/story"] + [""] * 10
    assert normalize_mention(row, {"2"}) is None


def test_qdrant_event_ids_pages_results():
    records = [
        {
            "id": f"gdelt:{event_id}",
            "payload": {"gdelt_event_id": event_id},
        }
        for event_id in range(1, 8)
    ]
    store = FakeStore(records)

    result = _qdrant_event_ids(store, page_size=2)

    assert result == {"1", "2", "3", "4", "5", "6", "7"}
    assert [call["offset"] for call in store.calls] == [None, 2, 4, 6]
    assert all(call["limit"] == 2 for call in store.calls)


def test_qdrant_event_ids_caps_page_size():
    store = FakeStore([])

    _qdrant_event_ids(store, page_size=10_000)

    assert store.calls[0]["limit"] == MAX_QDRANT_SCROLL_PAGE_SIZE
