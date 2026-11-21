import json

from services.account_activity import normalize_account_activity


def test_normalize_account_activity_extracts_nested_order_state():
    row = {
        "seq": 42,
        "1": "account-123",
        "2": "OrderEntryRequest",
        "3": json.dumps({"order": {"orderId": 987, "status": "PARTIALLY_FILLED"}}),
    }

    result = normalize_account_activity(row, packet_ts=1_700_000_000_000)

    assert result["account_number"] == "account-123"
    assert result["sequence_number"] == 42
    assert result["message_type"] == "ORDERENTRYREQUEST"
    assert result["broker_order_id"] == 987
    assert result["order_status"] == "partially_filled"
    assert result["event_ts"] == 1_700_000_000_000
    assert len(result["event_key"]) == 64


def test_normalize_account_activity_keeps_unknown_payload_without_inventing_status():
    result = normalize_account_activity(
        {"seq": "3", "1": "account", "2": "NOTICE", "3": "not-json"},
        packet_ts=123,
    )

    assert result["payload"]["message_data"] == {"raw_text": "not-json"}
    assert result["broker_order_id"] is None
    assert result["order_status"] is None


def test_account_activity_event_key_deduplicates_replayed_content():
    row = {"seq": 8, "1": "account", "2": "ORDER", "3": '{"orderId":1,"status":"FILLED"}'}

    first = normalize_account_activity(row, packet_ts=100)
    replay = normalize_account_activity(row, packet_ts=200)

    assert first["event_key"] == replay["event_key"]
