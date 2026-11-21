import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from schwab.streaming_client import SchwabWebSocketClient
from schwab import streaming_client


def _client():
    prefs = MagicMock()
    prefs.get_streamer_credentials.return_value = {
        "socket_url": "wss://stream.example/ws",
        "customer_id": "customer",
        "correlation_id": "correlation",
        "channel": "channel",
        "function_id": "function",
    }
    return SchwabWebSocketClient(prefs, "token")


def test_login_waits_for_successful_admin_response():
    client = _client()
    client.websocket = MagicMock()
    client.websocket.send = AsyncMock()
    client.websocket.recv = AsyncMock(return_value=json.dumps({
        "response": [{
            "service": "ADMIN",
            "command": "LOGIN",
            "content": {"code": 0, "msg": "Login succeeded"},
        }]
    }))

    asyncio.run(client._login())

    client.websocket.send.assert_awaited_once()
    client.websocket.recv.assert_awaited_once()


def test_login_denial_fails_before_subscriptions():
    client = _client()
    client.websocket = MagicMock()
    client.websocket.send = AsyncMock()
    client.websocket.recv = AsyncMock(return_value=json.dumps({
        "response": [{
            "service": "ADMIN",
            "command": "LOGIN",
            "content": {"code": 3, "msg": "Login denied"},
        }]
    }))

    with pytest.raises(PermissionError, match="Login denied"):
        asyncio.run(client._login())


def test_run_uses_existing_connection_instead_of_opening_a_second_one():
    client = _client()

    class ExistingSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise asyncio.CancelledError()

        async def close(self):
            return None

    client.websocket = ExistingSocket()
    client.connect = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.run(AsyncMock()))

    client.connect.assert_not_awaited()


def test_successful_reconnect_resets_connection_scoped_sequence_numbers(monkeypatch):
    client = _client()
    socket = MagicMock()
    connect = AsyncMock(return_value=socket)
    monkeypatch.setattr(streaming_client.websockets, "connect", connect)
    client._login = AsyncMock()
    client._resubscribe_all = AsyncMock()
    client._subs = [{"service": "CHART_EQUITY", "keys": "NVDA", "fields": "0,1,2"}]
    client._last_seq = {"CHART_EQUITY": 98_765}

    asyncio.run(client.connect())

    assert client._last_seq == {}
    client._login.assert_awaited_once()
    client._resubscribe_all.assert_awaited_once()


def test_book_subscription_uses_streamer_credentials_and_documented_fields():
    client = _client()
    client.websocket = MagicMock()
    client.websocket.send = AsyncMock()

    asyncio.run(client.subscribe_nasdaq_book(["F", "AAPL"]))

    payload = json.loads(client.websocket.send.await_args.args[0])
    request = payload["requests"][0]
    assert request["service"] == "NASDAQ_BOOK"
    assert request["SchwabClientCustomerId"] == "customer"
    assert request["SchwabClientCorrelId"] == "correlation"
    assert request["parameters"] == {"keys": "F,AAPL", "fields": "0,1,2,3"}


def test_new_subs_replaces_remembered_symbols_for_same_service():
    client = _client()
    client.websocket = MagicMock()
    client.websocket.send = AsyncMock()

    asyncio.run(client.subscribe_levelone_equities(["AAPL"], [0, 1, 2]))
    asyncio.run(client.subscribe_levelone_equities(["F"], [0, 1, 2]))

    assert [item for item in client._subs if item["service"] == "LEVELONE_EQUITIES"] == [{
        "service": "LEVELONE_EQUITIES",
        "keys": "F",
        "fields": "0,1,2",
    }]


def test_successful_replacement_subscription_resets_only_its_service_sequence():
    client = _client()
    client._last_seq = {
        "CHART_EQUITY\x1fMARA": 164,
        "CHART_EQUITY\x1fPATH": 53,
        "LEVELONE_EQUITIES\x1fMARA": 90_000,
    }

    client._apply_control_responses([{
        "service": "CHART_EQUITY",
        "command": "SUBS",
        "content": {"code": 0, "msg": "SUBS command succeeded"},
    }])

    assert client._last_seq == {"LEVELONE_EQUITIES\x1fMARA": 90_000}


def test_failed_replacement_subscription_keeps_sequence_high_water_mark():
    client = _client()
    client._last_seq = {"CHART_EQUITY\x1fMARA": 164}

    client._apply_control_responses([{
        "service": "CHART_EQUITY",
        "command": "SUBS",
        "content": {"code": 22, "msg": "SUBS command failed"},
    }])

    assert client._last_seq == {"CHART_EQUITY\x1fMARA": 164}


def test_first_lower_sequence_after_ack_is_delivered():
    client = _client()
    client._last_seq = {"CHART_EQUITY\x1fAMC": 164}
    on_data = AsyncMock()
    client._apply_control_responses([{
        "service": "CHART_EQUITY",
        "command": "SUBS",
        "content": {"code": 0},
    }])

    asyncio.run(client._dispatch_data([{
        "service": "CHART_EQUITY",
        "content": [{"seq": 1, "key": "AMC", "7": 1_790_086_800_000}],
    }], on_data))

    on_data.assert_awaited_once()
    assert client._last_seq["CHART_EQUITY\x1fAMC"] == 1


def test_equal_sequences_for_different_symbols_are_both_delivered():
    client = _client()
    on_data = AsyncMock()
    packet = [{
        "service": "CHART_EQUITY",
        "content": [
            {"seq": 7, "key": "AMC"},
            {"seq": 7, "key": "PATH"},
            {"seq": 7, "key": "AMC"},
        ],
    }]

    asyncio.run(client._dispatch_data(packet, on_data))

    assert on_data.await_count == 2
    assert client._last_seq == {
        "CHART_EQUITY\x1fAMC": 7,
        "CHART_EQUITY\x1fPATH": 7,
    }
