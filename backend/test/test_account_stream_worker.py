import asyncio
from unittest.mock import AsyncMock, MagicMock, call

from workers import account_stream_worker


class _StopReconnectLoop(BaseException):
    pass


def test_fast_capture_only_for_fresh_target_candidates_and_held_subscriptions(monkeypatch):
    monkeypatch.setattr(account_stream_worker.time, "time", lambda: 1000)
    monkeypatch.setattr(account_stream_worker, "list_active_position_symbols", lambda **kw: ["HELD", "UNSUBSCRIBED"])
    monkeypatch.setattr(account_stream_worker.persistent_store, "enabled", lambda: True)
    def candidate(symbol, **overrides):
        return {"symbol": symbol, "ts": 999, "last": 10, "rvol": 5,
                "company": {"float_shares": 1_000_000}, "price_range": [1, 20],
                "decision": {"guardrails": {"min_rvol": 4, "max_float": 20_000_000}}, **overrides}
    rows = [candidate("GOOD"), candidate("EXPENSIVE", last=30), candidate("LOW", rvol=2),
            candidate("STALE", ts=1), candidate("FUTURE", ts=1001), candidate("UNKNOWN", company={}),
            candidate("HIGH", company={"float_shares": 30_000_000}), candidate("NAN", rvol=float("nan")),
            candidate("INF", rvol=float("inf")), candidate("UNSUBSCRIBED")]
    monkeypatch.setattr(account_stream_worker.persistent_store, "list_scanner_results", lambda **kw: rows)
    subscribed = [r["symbol"] for r in rows if r["symbol"] != "UNSUBSCRIBED"] + ["HELD"]
    assert account_stream_worker._capture_symbols(subscribed) == {"GOOD", "HELD"}


def test_priority_recording_cadence_does_not_change_background_or_batching(monkeypatch):
    monkeypatch.setattr(account_stream_worker, "QUOTE_OBSERVATION_INTERVAL_MS", 5000)
    monkeypatch.setattr(account_stream_worker, "BOOK_OBSERVATION_INTERVAL_MS", 10000)
    monkeypatch.setattr(account_stream_worker, "PRIORITY_OBSERVATION_INTERVAL_MS", 1000)
    assert account_stream_worker._observation_interval("levelone_quote", "A", {"A"}) == 1000
    assert account_stream_worker._observation_interval("level2_book", "A", {"A"}) == 1000
    assert account_stream_worker._observation_interval("levelone_quote", "B", {"A"}) == 5000
    assert account_stream_worker._observation_interval("level2_book", "B", {"A"}) == 10000
    samples = {}
    assert account_stream_worker._sample_due(samples, "level2_book", "A", 10000, 1000)
    assert not account_stream_worker._sample_due(samples, "level2_book", "A", 10500, 1000)
    assert account_stream_worker._sample_due(samples, "level2_book", "A", 11000, 1000)


def test_reconcile_order_intent_uses_authoritative_broker_snapshot(monkeypatch):
    client = MagicMock()
    client.get_order.return_value = {"orderId": 77, "status": "CANCELED"}
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(account_stream_worker, "SchwabOrderClient", client_factory)
    record = MagicMock(return_value={"matched": True, "fills_created": 0})
    monkeypatch.setattr(account_stream_worker, "record_order_snapshot", record)
    preferences = MagicMock()

    result = account_stream_worker._reconcile_order_intent(
        {"account_id": "encrypted-account", "broker_order_id": 77},
        access_token="token",
        preferences=preferences,
    )

    client_factory.assert_called_once_with(preferences, access_token="token")
    client.get_order.assert_called_once_with("encrypted-account", 77)
    snapshot = record.call_args.args[0]
    assert snapshot["broker_order_id"] == 77
    assert snapshot["status"] == "canceled"
    assert result["matched"] is True


def test_recover_unidentified_intent_attaches_unique_match_before_recording(monkeypatch):
    client = MagicMock()
    client.get_orders.return_value = [{"orderId": 77, "status": "FILLED"}]
    monkeypatch.setattr(
        account_stream_worker, "SchwabOrderClient", MagicMock(return_value=client)
    )
    monkeypatch.setattr(
        account_stream_worker,
        "ambiguous_order_query_window",
        MagicMock(return_value=("from", "to")),
    )
    monkeypatch.setattr(
        account_stream_worker,
        "match_ambiguous_order",
        MagicMock(return_value={"orderId": 77, "status": "FILLED"}),
    )
    monkeypatch.setattr(
        account_stream_worker,
        "normalize_schwab_order",
        MagicMock(return_value={"broker_order_id": 77, "status": "filled"}),
    )
    attach = MagicMock(return_value={"broker_order_id": 77})
    record = MagicMock(return_value={"matched": True, "fills_created": 1})
    monkeypatch.setattr(account_stream_worker, "attach_broker_order_identity", attach)
    monkeypatch.setattr(account_stream_worker, "record_order_snapshot", record)
    intent = {"idempotency_key": "exit:abcdefgh", "account_id": "account"}

    result = account_stream_worker._recover_unidentified_order_intent(
        intent, access_token="token", preferences=MagicMock()
    )

    client.get_orders.assert_called_once_with("account", "from", "to", max_results=100)
    attach.assert_called_once_with("exit:abcdefgh", broker_order_id=77)
    record.assert_called_once_with({"broker_order_id": 77, "status": "filled"})
    assert result["matched"] is True


def test_recover_unidentified_intent_does_not_guess(monkeypatch):
    client = MagicMock()
    client.get_orders.return_value = []
    monkeypatch.setattr(
        account_stream_worker, "SchwabOrderClient", MagicMock(return_value=client)
    )
    monkeypatch.setattr(
        account_stream_worker,
        "ambiguous_order_query_window",
        MagicMock(return_value=("from", "to")),
    )
    monkeypatch.setattr(
        account_stream_worker, "match_ambiguous_order", MagicMock(return_value=None)
    )
    attach = MagicMock()
    monkeypatch.setattr(account_stream_worker, "attach_broker_order_identity", attach)

    result = account_stream_worker._recover_unidentified_order_intent(
        {"idempotency_key": "exit:abcdefgh", "account_id": "account"},
        access_token="token",
        preferences=MagicMock(),
    )

    assert result == {"matched": False, "reason": "no_unique_exact_match"}
    attach.assert_not_called()


def test_chart_observation_retains_event_and_receipt_times():
    observation = account_stream_worker._durable_chart_observation({
        "symbol": "F",
        "chart_time_ms": 1_800_000_000_000,
        "received_at_ms": 1_800_000_000_987,
        "open": 14.0,
        "high": 14.2,
        "low": 13.9,
        "close": 14.1,
        "volume": 1234,
        "source": "schwab_stream",
    })

    assert observation["event_ts"] == 1_800_000_000
    assert observation["received_ts"] == 1_800_000_000
    assert observation["price"] == 14.1
    assert observation["observation_type"] == "minute_candle"


def test_levelone_observation_uses_quote_midpoint_and_latest_market_time():
    observation = account_stream_worker._durable_levelone_observation({
        "symbol": "NVDA",
        "bid": 120.0,
        "ask": 120.04,
        "last": 120.03,
        "mark": 120.02,
        "total_volume": 98765,
        "quote_time_ms": 1_800_000_001_000,
        "trade_time_ms": 1_800_000_000_000,
        "bid_time_ms": 1_800_000_001_100,
        "ask_time_ms": 1_800_000_001_200,
        "received_at_ms": 1_800_000_001_987,
    })

    assert observation["observation_type"] == "levelone_quote"
    assert observation["event_ts"] == 1_800_000_001
    assert observation["received_ts"] == 1_800_000_001
    assert abs(observation["price"] - 120.02) < 1e-9
    assert observation["bid"] == 120.0
    assert observation["ask"] == 120.04
    assert observation["volume"] == 98765


def test_book_observation_retains_selected_depth_and_top_of_book():
    normalized = {
        "symbol": "F",
        "service": "NYSE_BOOK",
        "market_time_ms": 1_800_000_002_000,
        "received_at_ms": 1_800_000_002_500,
        "bids": [{"price": 14.10, "size": 600}],
        "asks": [{"price": 14.12, "size": 400}],
    }

    observation = account_stream_worker._durable_book_observation(normalized)

    assert observation["observation_type"] == "level2_book"
    assert observation["event_ts"] == 1_800_000_002
    assert observation["bid"] == 14.10
    assert observation["ask"] == 14.12
    assert observation["price"] == 14.11
    assert observation["payload"] is normalized


def test_observation_sampling_is_independent_by_symbol_and_type():
    sampled = {}

    assert account_stream_worker._sample_due(
        sampled, "levelone_quote", "F", 10_000, 5_000
    )
    assert not account_stream_worker._sample_due(
        sampled, "levelone_quote", "F", 14_999, 5_000
    )
    assert account_stream_worker._sample_due(
        sampled, "levelone_quote", "F", 15_000, 5_000
    )
    assert account_stream_worker._sample_due(
        sampled, "level2_book", "F", 15_000, 10_000
    )
    assert account_stream_worker._sample_due(
        sampled, "levelone_quote", "NVDA", 15_000, 5_000
    )


def test_stream_symbols_reserves_benchmark_after_position_symbols(monkeypatch):
    monkeypatch.setattr(
        account_stream_worker, "list_active_position_symbols",
        MagicMock(return_value=["LIVE"]),
    )
    monkeypatch.setattr(account_stream_worker, "REQUIRED_SYMBOLS", ["SPY"])
    monkeypatch.setattr(
        account_stream_worker.persistent_store, "enabled", MagicMock(return_value=False)
    )
    monkeypatch.setenv("SCHWAB_STREAM_SYMBOLS", "")

    assert account_stream_worker._stream_symbols() == ["LIVE", "SPY"]


def test_stream_symbols_fills_capacity_from_full_scanner_watchlist(monkeypatch):
    monkeypatch.setattr(
        account_stream_worker, "list_active_position_symbols",
        MagicMock(return_value=["LIVE"]),
    )
    monkeypatch.setattr(account_stream_worker, "REQUIRED_SYMBOLS", ["SPY"])
    monkeypatch.setattr(account_stream_worker, "MAX_SYMBOLS", 6)
    monkeypatch.setattr(
        account_stream_worker, "symbols_requiring_outcomes",
        MagicMock(return_value=["OUTCOME"]),
    )
    monkeypatch.setattr(
        account_stream_worker.persistent_store, "enabled", MagicMock(return_value=True)
    )
    list_results = MagicMock(
        side_effect=lambda **kwargs: (
            [{"symbol": "FOCUS"}]
            if kwargs.get("focus_only")
            else [
                {"symbol": "FOCUS"},
                {"symbol": "WATCH1"},
                {"symbol": "WATCH2"},
            ]
        )
    )
    monkeypatch.setattr(
        account_stream_worker.persistent_store, "list_scanner_results", list_results
    )
    monkeypatch.setenv("SCHWAB_STREAM_SYMBOLS", "")

    assert account_stream_worker._stream_symbols() == [
        "LIVE", "SPY", "FOCUS", "OUTCOME", "WATCH1", "WATCH2"
    ]
    assert list_results.call_args_list == [
        call(focus_only=True),
        call(),
    ]
    monkeypatch.setattr(account_stream_worker, "MAX_SYMBOLS", 3)
    assert account_stream_worker._stream_symbols() == ["LIVE", "SPY", "FOCUS"]
    monkeypatch.setenv('LEGACY_RESEARCH_ENABLED', 'false')
    monkeypatch.setattr(account_stream_worker, 'MAX_SYMBOLS', 6)
    account_stream_worker.symbols_requiring_outcomes.reset_mock()
    assert account_stream_worker._stream_symbols() == ['LIVE', 'SPY', 'FOCUS', 'WATCH1', 'WATCH2']
    account_stream_worker.symbols_requiring_outcomes.assert_not_called()


def test_symbol_refresh_recovers_after_transient_subscription_failure(monkeypatch):
    state = {
        "symbols": ["OLD"],
        "symbol_refresh_errors": 0,
        "last_symbol_refresh_ms": None,
        "last_symbol_refresh_error": None,
    }
    sleeps = 0

    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 2:
            raise asyncio.CancelledError()

    selections = MagicMock(side_effect=[["FIRST"], ["SECOND"]])
    attempts = 0

    async def subscribe(symbols):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary subscription failure")
        state["symbols"] = symbols

    monkeypatch.setattr(account_stream_worker.asyncio, "sleep", sleep)
    monkeypatch.setattr(account_stream_worker, "_stream_symbols", selections)

    try:
        asyncio.run(account_stream_worker._refresh_stream_symbols(state, subscribe))
    except asyncio.CancelledError:
        pass

    assert attempts == 2
    assert state["symbols"] == ["SECOND"]
    assert state["symbol_refresh_errors"] == 1
    assert state["last_symbol_refresh_error"] is None
    assert state["last_symbol_refresh_ms"] is not None


def test_capture_refresh_failure_clears_priority_without_losing_subscriptions(monkeypatch):
    state = {"symbols": ["A"], "capture_symbols": {"A"}, "symbol_refresh_errors": 0}
    sleeps = 0
    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            assert state["capture_symbols"] == set()
        if sleeps > 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(account_stream_worker.asyncio, "sleep", sleep)
    monkeypatch.setattr(account_stream_worker, "_stream_symbols", lambda: ["A"])
    monkeypatch.setattr(account_stream_worker, "_capture_symbols", MagicMock(side_effect=[ConnectionError("database unavailable"), {"A"}]))
    subscribe = AsyncMock()
    try:
        asyncio.run(account_stream_worker._refresh_stream_symbols(state, subscribe))
    except asyncio.CancelledError:
        pass
    assert state["capture_symbols"] == {"A"}
    assert state["symbols"] == ["A"]
    assert state["symbol_refresh_errors"] == 1
    assert state["last_symbol_refresh_error"] is None
    subscribe.assert_not_called()


def test_stream_failure_reports_error_and_reconnects(monkeypatch):
    attempts = 0

    async def run_session(_access_token):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("stream disconnected")
        raise _StopReconnectLoop()

    heartbeat = MagicMock()
    monkeypatch.setattr(account_stream_worker, "_heartbeat", heartbeat)
    monkeypatch.setattr(
        account_stream_worker,
        "get_market_data_access_token",
        MagicMock(return_value="token"),
    )
    monkeypatch.setattr(account_stream_worker, "_run_authenticated_stream", run_session)
    monkeypatch.setattr(account_stream_worker.asyncio, "sleep", AsyncMock())

    try:
        asyncio.run(account_stream_worker.run_forever())
    except _StopReconnectLoop:
        pass

    assert attempts == 2
    heartbeat.assert_called_once_with("error", error="stream disconnected")
