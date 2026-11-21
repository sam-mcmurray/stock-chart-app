import importlib
import json
import os
import sys
import types
import pytest
from unittest.mock import MagicMock, patch


sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from services import agent_price_history


def _module(**attrs):
    mod = types.ModuleType("stub")
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class _RouterStub:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def post(self, *args, **kwargs):
        return self.get(*args, **kwargs)


class _JSONResponseStub:
    def __init__(self, content, status_code=200):
        self.body = json.dumps(content).encode()
        self.status_code = status_code


class _HTTPExceptionStub(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _import_agent_api(redis_mock):
    modules = {
        "services.agent_price_history": agent_price_history,
        "fastapi": _module(
            APIRouter=_RouterStub,
            Query=lambda default=None, **kwargs: default,
            HTTPException=_HTTPExceptionStub,
            Depends=lambda dependency=None: dependency,
            Header=lambda default=None, **kwargs: default,
        ),
        "fastapi.responses": _module(JSONResponse=_JSONResponseStub),
        "redis": _module(from_url=MagicMock(return_value=redis_mock)),
        "pandas": _module(),
        "torch": _module(no_grad=MagicMock()),
        "drl_agent": _module(),
        "drl_agent.environment": _module(StockTradingEnv=MagicMock()),
        "drl_agent.agent": _module(PPOAgent=MagicMock()),
        "drl_agent.committee": _module(TradingCommittee=MagicMock()),
        "drl_agent.risk": _module(evaluate_stop_loss=MagicMock()),
        "schwab": _module(),
        "schwab.orders": _module(SchwabOrderClient=MagicMock()),
        "schwab.user_preferences": _module(UserPreferences=MagicMock()),
        "schwab.accounts": _module(SchwabAccountClient=MagicMock()),
        "schwab.market_data": _module(SchwabMarketDataClient=MagicMock()),
        "routers.auth": _module(
            require_access_token=MagicMock(),
            get_trading_authorization_status=MagicMock(return_value={}),
        ),
        "routers.scanners_api": _module(
            _load_price_history=MagicMock(),
            _load_agent=MagicMock(),
            _Memory=MagicMock(),
        ),
        "services": _module(
            persistent_store=_module(healthcheck=MagicMock(return_value=True))
        ),
        "services.feature_store": _module(
            list_feature_snapshots=MagicMock(return_value=[]),
            record_feature_snapshot=MagicMock(),
        ),
        "services.market_signal": _module(market_signal=MagicMock()),
        "services.semantic_signal": _module(semantic_signal=MagicMock()),
        "services.decision_engine": _module(
            DecisionConfig=MagicMock(),
            news_sentiment=MagicMock(),
            trade_plan=MagicMock(),
        ),
        "services.trade_risk": _module(
            RiskLimits=MagicMock(),
            calculate_long_position_size=MagicMock(),
        ),
        "services.account_risk_ledger": _module(
            get_account_risk_summary=MagicMock(return_value={
                "daily_realized_pnl": -25.0,
                "aggregate_open_risk": 125.0,
                "risk_state_complete": True,
                "positions_at_risk": 1,
                "incomplete_reasons": [],
            }),
        ),
        "services.entry_market_gate": _module(
            EntryMarketLimits=MagicMock(),
            evaluate_entry_market_gate=MagicMock(),
            normalize_schwab_equity_hours=MagicMock(),
            normalize_schwab_equity_quote=MagicMock(),
        ),
        "services.market_stream_store": _module(
            load_market_stream_snapshot=MagicMock(return_value={
                "symbol": "F",
                "levelone": {"bid": 14, "ask": 14.01},
                "book_metrics": {"imbalance": 0.2},
                "candles": [],
            }),
        ),
        "services.order_flow_confirmation": _module(
            OrderFlowLimits=MagicMock(),
            evaluate_order_flow_confirmation=MagicMock(
                return_value=MagicMock(
                    to_dict=MagicMock(return_value={"allowed": True})
                )
            ),
        ),
        "services.local_exit_readiness": _module(
            evaluate_local_exit_readiness=MagicMock(
                return_value=MagicMock(
                    to_dict=MagicMock(return_value={"ready": False, "reasons": ["execution_disabled"]})
                )
            ),
        ),
        "services.trade_journal": _module(
            IdempotencyConflict=RuntimeError,
            ensure_entry_position=MagicMock(),
            list_order_intents=MagicMock(return_value=[]),
            reserve_order_intent=MagicMock(),
            update_order_intent=MagicMock(),
        ),
        "workers": _module(),
        "workers.stop_loss_worker": _module(
            H_STOP_LOSS_HEARTBEAT="agent:stop_loss:heartbeat",
            K_STOP_LOSS_EVENTS="agent:stop_loss:events",
        ),
        "workers.scanner_worker": _module(
            _current_session_rvol=MagicMock(),
            _get_daily=MagicMock(),
            _schwab_market_client=MagicMock(),
            _macd_open=MagicMock(),
            _recent_articles=MagicMock(),
        ),
    }
    with patch.dict(sys.modules, modules):
        sys.modules.pop("routers.agent_api", None)
        mod = importlib.import_module("routers.agent_api")
        sys.modules.pop("routers.agent_api", None)
        routers_pkg = sys.modules.get("routers")
        if routers_pkg is not None and getattr(routers_pkg, "agent_api", None) is mod:
            delattr(routers_pkg, "agent_api")
        return mod


def _json_body(response):
    return json.loads(response.body.decode())


def test_agent_rvol_adapter_uses_current_quote_and_requires_valid_volume():
    agent_api = _import_agent_api(MagicMock())
    agent_api._current_session_rvol.return_value = (3.5, {"valid": True})
    frame = object()
    quote = {"quote_time_ms": 1800000000123, "total_volume": 3500, "total_volume_valid": True}
    assert agent_api._agent_current_rvol("TEST", frame, quote) == (3.5, {"valid": True})
    args = agent_api._current_session_rvol.call_args.args
    assert args[1] is frame
    assert args[3] == {"market_ts": 1800000000.123, "total_volume": 3500}
    quote["total_volume_valid"] = False
    agent_api._agent_current_rvol("TEST", frame, quote)
    assert agent_api._current_session_rvol.call_args.args[3]["total_volume"] is None


def test_agent_rvol_entry_gate_rejects_unavailable_even_at_zero_threshold():
    agent_api = _import_agent_api(MagicMock())
    assert agent_api._rvol_entry_reason(None, 0) == "rvol_unavailable"
    assert agent_api._rvol_entry_reason(2.9, 3).startswith("rvol_below_threshold")
    assert agent_api._rvol_entry_reason(3, 3) is None


def test_agent_history_loader_fails_closed_on_provider_failure():
    agent_api = _import_agent_api(MagicMock())
    agent_api._get_daily.side_effect = RuntimeError("provider unavailable")
    with pytest.raises(_HTTPExceptionStub) as exc:
        agent_api._load_agent_daily_history("TEST", object())
    assert exc.value.status_code == 503
    assert exc.value.detail == "schwab_history_unavailable"


def test_agent_history_loader_rejects_missing_history():
    agent_api = _import_agent_api(MagicMock())
    agent_api._get_daily.return_value = None
    with pytest.raises(_HTTPExceptionStub) as exc:
        agent_api._load_agent_daily_history("TEST", object())
    assert exc.value.status_code == 503
    assert exc.value.detail == "daily_history_insufficient"


def test_action_route_uses_latest_window_before_loading_policy(monkeypatch):
    agent_api = _import_agent_api(MagicMock())
    env = agent_api.StockTradingEnv.return_value
    env._get_rag_observation.return_value = (0, [], 0)
    monkeypatch.setattr(agent_api, "_local_exit_readiness", lambda: {})
    monkeypatch.setattr(agent_api, "_load_agent_daily_history", lambda *args: MagicMock())
    monkeypatch.setattr(agent_api, "_attach_gdelt_mentions", lambda *args: [])
    monkeypatch.setattr(agent_api, "_live_entry_market_snapshot", lambda *args: {
        "quote": {"last": 5}, "market_hours": {}, "now_ms": 1})
    monkeypatch.setattr(agent_api, "_agent_current_rvol", lambda *args: (3, {"valid": True}))
    monkeypatch.setattr(agent_api, "_schwab_account_risk_snapshot", lambda *args, **kwargs: {
        "equity": 500, "buying_power": 400, "daily_realized_pnl": -5, "aggregate_open_risk": 0,
        "positions": [{"instrument": {"symbol": "TEST"}, "longQuantity": 20, "averagePrice": 5}]})
    monkeypatch.setattr(agent_api, "_event_time_market_observations", lambda *args: [])
    monkeypatch.setattr(agent_api, "_market_stream_snapshot", lambda *args, **kwargs: {})
    class ReachedPolicy(Exception):
        pass
    def capture_policy(*args):
        env.reset.assert_called_once_with(latest_window=True, account_state={
            "balance": 400, "equity": 500, "realized_pnl": -5,
            "quantity": 20, "average_price": 5})
        env.step.assert_not_called()
        raise ReachedPolicy()
    monkeypatch.setattr(agent_api, "_load_agent", capture_policy)
    with pytest.raises(ReachedPolicy):
        agent_api.act(symbol="TEST", simulate=True, access_token="test-token")


def test_attach_gdelt_mentions_batches_events_and_preserves_articles():
    agent_api = _import_agent_api(MagicMock())
    loader = MagicMock(return_value=[
        {"event_id": "123", "url": "https://one.example/a", "mention_ts": 1},
        {"event_id": "456", "url": "https://two.example/b", "mention_ts": 2},
    ])
    agent_api.persistent_store.list_gdelt_mentions_for_events = loader

    result = agent_api._attach_gdelt_mentions([
        {"title": "First", "event_ids": ["123", "456"]},
        {"title": "Second", "event_id": "456"},
        {"title": "No event"},
    ])

    loader.assert_called_once_with(["123", "456"])
    assert len(result[0]["gdelt_mentions"]) == 2
    assert len(result[1]["gdelt_mentions"]) == 1
    assert result[2]["gdelt_mentions"] == []


def test_stop_loss_status_includes_heartbeat_age_and_execution_count():
    redis_mock = MagicMock()
    redis_mock.get.return_value = json.dumps({"status": "ok", "ts": 1700000000, "checked": 3})

    def lrange_side_effect(key, start, end):
        if key == "agent:stop_loss:events":
            return [
                json.dumps({"symbol": "AAPL", "status": "dry_run"}),
                "not-json",
            ]
        if key == "agent:exec_log":
            return [
                json.dumps({"symbol": "AAPL", "status": "submitted"}),
                json.dumps({"symbol": "MSFT", "status": "error"}),
                "bad-json",
            ]
        return []

    redis_mock.lrange.side_effect = lrange_side_effect
    agent_api = _import_agent_api(redis_mock)

    with patch.object(agent_api.time, "time", return_value=1700000065):
        data = _json_body(agent_api.stop_loss_status())

    assert data["heartbeat"]["status"] == "ok"
    assert data["heartbeat_age_seconds"] == 65
    assert data["recent_events"] == [{"symbol": "AAPL", "status": "dry_run"}]
    assert data["recent_execution_count"] == 2
    assert data["execute_trades"] is False
    assert data["monitor_enabled"] is True


def test_recent_executions_returns_valid_json_rows_only():
    redis_mock = MagicMock()
    redis_mock.lrange.return_value = [
        json.dumps({"symbol": "AAPL", "status": "submitted"}),
        json.dumps(["not", "an", "object"]),
        "not-json",
        json.dumps({"symbol": "TSLA", "status": "error"}),
    ]
    agent_api = _import_agent_api(redis_mock)

    data = _json_body(agent_api.recent_executions(limit=10))

    assert data == [
        {"symbol": "AAPL", "status": "submitted"},
        {"symbol": "TSLA", "status": "error"},
    ]
    redis_mock.lrange.assert_called_with("agent:exec_log", 0, 9)


def test_exit_controller_status_is_explicitly_shadow_only():
    redis_mock = MagicMock()
    redis_mock.get.return_value = json.dumps(
        {"status": "ok", "ts": 1700000000, "checked": 1, "hold": 1}
    )
    redis_mock.lrange.return_value = [
        json.dumps({"position_id": "pos:abc", "action": "take_partial"})
    ]
    agent_api = _import_agent_api(redis_mock)

    with patch.object(agent_api.time, "time", return_value=1700000005):
        data = _json_body(agent_api.exit_controller_status())

    assert data["heartbeat_age_seconds"] == 5
    assert data["execution_mode"] == "shadow"
    assert data["execute_trades"] is False
    assert data["recent_events"][0]["action"] == "take_partial"


def test_account_stream_status_reports_shared_relay_health():
    redis_mock = MagicMock()
    redis_mock.get.return_value = json.dumps({"status": "ok", "ts": 1700000000})
    agent_api = _import_agent_api(redis_mock)

    with patch.object(agent_api.time, "time", return_value=1700000009):
        data = _json_body(agent_api.account_stream_status())

    assert data["heartbeat_age_seconds"] == 9
    assert data["shared_connection"] is True
    assert data["browser_mode"] == "redis_relay"
    assert data["execute_trades"] is False


def test_market_stream_snapshot_is_normalized_and_marks_subscription():
    redis_mock = MagicMock()
    agent_api = _import_agent_api(redis_mock)

    data = _json_body(agent_api.market_stream_snapshot("f", candle_limit=120, access_token="token"))

    assert data["symbol"] == "F"
    assert data["subscribed"] is True
    assert data["order_flow_confirmation"] == {"allowed": True}
    agent_api.load_market_stream_snapshot.assert_called_once_with(
        redis_mock,
        symbol="F",
        levelone_key="schwab:stream:levelone",
        nasdaq_book_key="schwab:stream:nasdaq_book",
        nyse_book_key="schwab:stream:nyse_book",
        chart_key_prefix="schwab:stream:chart:",
        chart_limit=120,
    )


def test_market_stream_snapshot_fails_closed_when_redis_read_fails():
    redis_mock = MagicMock()
    agent_api = _import_agent_api(redis_mock)
    agent_api.load_market_stream_snapshot.side_effect = RuntimeError("redis unavailable")
    agent_api.evaluate_order_flow_confirmation.return_value.to_dict.return_value = {
        "allowed": False,
        "reasons": ["level2_missing"],
    }

    data = _json_body(agent_api.market_stream_snapshot("f", candle_limit=5, access_token="token"))

    assert data["symbol"] == "F"
    assert data["subscribed"] is False
    assert data["stream_error"] == "redis unavailable"
    assert data["order_flow_confirmation"] == {
        "allowed": False,
        "reasons": ["level2_missing"],
    }


def test_account_snapshot_does_not_fall_back_to_another_account():
    agent_api = _import_agent_api(MagicMock())
    client = agent_api.SchwabAccountClient.return_value
    client.get_all_accounts.return_value = [{"securitiesAccount": {"accountNumber": "other"}}]
    with pytest.raises(RuntimeError, match="Selected Schwab account unavailable"):
        agent_api._schwab_account_risk_snapshot("token", preferred_account="missing")


def test_account_snapshot_positions_belong_to_selected_account():
    agent_api = _import_agent_api(MagicMock())
    client = agent_api.SchwabAccountClient.return_value
    positions = [{"instrument": {"symbol": "TEST"}, "longQuantity": 3}]
    client.get_all_accounts.return_value = [
        {"securitiesAccount": {"accountNumber": "other", "positions": [{"longQuantity": 99}]}},
        {"securitiesAccount": {"accountNumber": "selected", "positions": positions}},
    ]
    result = agent_api._schwab_account_risk_snapshot("token", preferred_account="selected")
    assert result["account_number"] == "selected"
    assert result["positions"] == positions


def test_schwab_account_risk_snapshot_uses_conservative_live_balances():
    redis_mock = MagicMock()
    agent_api = _import_agent_api(redis_mock)
    account_client = agent_api.SchwabAccountClient.return_value
    account_client.get_all_accounts.return_value = [
        {
            "securitiesAccount": {
                "accountNumber": "123",
                "currentBalances": {
                    "liquidationValue": 50_000,
                    "availableFundsNonMarginableTrade": 8_000,
                    "buyingPower": 40_000,
                },
                "positions": [{"longQuantity": 10}, {"shortQuantity": 0}],
            }
        }
    ]
    account_client.get_account_numbers.return_value = [
        {"accountNumber": "123", "hashValue": "encrypted-123"}
    ]

    result = agent_api._schwab_account_risk_snapshot("token", preferred_account="123")

    assert result["equity"] == 50_000
    assert result["buying_power"] == 8_000
    assert result["open_position_count"] == 1
    assert result["daily_realized_pnl"] == -25.0
    assert result["aggregate_open_risk"] == 125.0
    assert result["risk_state_complete"] is True
    assert result["risk_account_id"] == "encrypted-123"
    agent_api.get_account_risk_summary.assert_called_once_with(account_id="encrypted-123")
    agent_api.SchwabAccountClient.assert_called_with(
        user_prefs=agent_api.UserPreferences.return_value,
        access_token="token",
        eager_load=False,
    )


def test_resolve_account_number_returns_schwab_encrypted_id():
    redis_mock = MagicMock()
    agent_api = _import_agent_api(redis_mock)
    account_client = agent_api.SchwabAccountClient.return_value
    account_client.get_account_numbers.return_value = [
        {"accountNumber": "123", "hashValue": "encrypted-123"},
        {"accountNumber": "456", "hashValue": "encrypted-456"},
    ]

    result = agent_api._resolve_account_number("token", preferred="456")

    assert result == "encrypted-456"


def test_resolve_account_number_rejects_unknown_preferred_account():
    redis_mock = MagicMock()
    agent_api = _import_agent_api(redis_mock)
    agent_api.SchwabAccountClient.return_value.get_account_numbers.return_value = [
        {"accountNumber": "123", "hashValue": "encrypted-123"},
    ]

    try:
        agent_api._resolve_account_number("token", preferred="missing")
    except _HTTPExceptionStub as exc:
        assert exc.status_code == 503
        assert "not found" in exc.detail
    else:
        raise AssertionError("Expected unknown account to fail closed")


def test_recent_order_intents_uses_authenticated_encrypted_account():
    redis_mock = MagicMock()
    agent_api = _import_agent_api(redis_mock)
    agent_api.SchwabAccountClient.return_value.get_account_numbers.return_value = [
        {"accountNumber": "123", "hashValue": "encrypted-123"},
    ]
    agent_api.list_order_intents.return_value = [{"status": "submitted", "symbol": "AAPL"}]

    data = _json_body(agent_api.recent_order_intents(limit=10, account_id="123", access_token="token"))

    assert data == {"orders": [{"status": "submitted", "symbol": "AAPL"}]}
    agent_api.list_order_intents.assert_called_once_with(account_id="encrypted-123", limit=10)
