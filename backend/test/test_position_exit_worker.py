from unittest.mock import MagicMock
import time

from workers import position_exit_worker


class _StopWorkerLoop(BaseException):
    pass


def _position():
    return {
        "position_id": "pos:abc",
        "symbol": "ABCD",
        "state": "open",
        "remaining_quantity": 100,
        "average_entry_price": 10,
        "hard_stop_price": 9,
        "active_stop_price": 9,
        "peak_price": 10,
        "partial_taken": False,
    }


def test_worker_persists_shadow_decision_without_placing_orders(monkeypatch):
    monkeypatch.setattr(
        position_exit_worker,
        "list_positions_for_protection",
        MagicMock(return_value=[{"position": _position(), "protective_intent": None}]),
    )
    evaluation = MagicMock()
    evaluation.to_dict.return_value = {
        "action": "take_partial",
        "quantity": 50,
        "reason": "partial_profit_target",
        "active_stop": 10,
        "peak_price": 11.5,
    }
    monkeypatch.setattr(
        position_exit_worker, "evaluate_position_exit", MagicMock(return_value=evaluation)
    )
    update = MagicMock(return_value={"updated": True, "reason": None})
    monkeypatch.setattr(position_exit_worker, "update_position_risk_state", update)
    worker = position_exit_worker.PositionExitWorker()
    worker.apply_stops = True
    monkeypatch.setattr(worker, "_snapshot", MagicMock(return_value={}))
    monkeypatch.setattr(worker, "_record_actionable", MagicMock())

    result = worker.run_once()

    assert result["checked"] == 1
    assert result["take_partial"] == 1
    assert result["stop_ratchets"] == 1
    assert result["execution_mode"] == "shadow"
    assert result["stop_apply_enabled"] is True
    update.assert_called_once()
    worker._record_actionable.assert_called_once()


def test_worker_skips_closed_positions(monkeypatch):
    closed = {**_position(), "state": "closed", "remaining_quantity": 0}
    monkeypatch.setattr(
        position_exit_worker,
        "list_positions_for_protection",
        MagicMock(return_value=[{"position": closed, "protective_intent": {}}]),
    )
    evaluate = MagicMock()
    monkeypatch.setattr(position_exit_worker, "evaluate_position_exit", evaluate)

    result = position_exit_worker.PositionExitWorker().run_once()

    assert result["checked"] == 0
    evaluate.assert_not_called()


def test_shadow_worker_does_not_mutate_position_stop(monkeypatch):
    monkeypatch.setattr(
        position_exit_worker,
        "list_positions_for_protection",
        MagicMock(return_value=[{"position": _position(), "protective_intent": None}]),
    )
    evaluation = MagicMock()
    evaluation.to_dict.return_value = {
        "action": "hold",
        "quantity": 0,
        "reason": None,
        "active_stop": 10,
        "peak_price": 11.5,
    }
    monkeypatch.setattr(
        position_exit_worker, "evaluate_position_exit", MagicMock(return_value=evaluation)
    )
    update = MagicMock()
    monkeypatch.setattr(position_exit_worker, "update_position_risk_state", update)
    worker = position_exit_worker.PositionExitWorker()
    worker.apply_stops = False
    monkeypatch.setattr(worker, "_snapshot", MagicMock(return_value={}))

    result = worker.run_once()

    assert result["hold"] == 1
    assert result["stop_apply_enabled"] is False
    update.assert_not_called()


def test_exit_submission_requires_both_interlocks(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTE_TRADES", "true")
    monkeypatch.setenv("AGENT_EXIT_SUBMIT_ENABLED", "false")
    assert position_exit_worker.PositionExitWorker().submit_exits is False

    monkeypatch.setenv("AGENT_EXIT_SUBMIT_ENABLED", "true")
    assert position_exit_worker.PositionExitWorker().submit_exits is True


def test_worker_uses_rest_quote_when_stream_snapshot_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        position_exit_worker,
        "list_positions_for_protection",
        MagicMock(return_value=[{"position": _position(), "protective_intent": None}]),
    )
    monkeypatch.setattr(
        position_exit_worker,
        "update_position_risk_state",
        MagicMock(return_value={"updated": True}),
    )
    market_client = MagicMock()
    market_client.get_quote.return_value = {
        "ABCD": {
            "realtime": True,
            "assetMainType": "EQUITY",
            "quote": {
                "bidPrice": 8.49,
                "askPrice": 8.51,
                "lastPrice": 8.50,
                "mark": 8.50,
                "quoteTime": int(time.time() * 1000),
                "tradeTime": int(time.time() * 1000),
                "securityStatus": "Normal",
            },
        }
    }
    monkeypatch.setattr(
        position_exit_worker,
        "SchwabMarketDataClient",
        MagicMock(return_value=market_client),
    )
    monkeypatch.setattr(
        position_exit_worker, "get_trading_access_token", MagicMock(return_value="token")
    )
    monkeypatch.setattr(position_exit_worker, "UserPreferences", MagicMock())
    worker = position_exit_worker.PositionExitWorker()
    monkeypatch.setattr(worker, "_snapshot", MagicMock(side_effect=RuntimeError("redis down")))
    record = MagicMock()
    monkeypatch.setattr(worker, "_record_actionable", record)

    result = worker.run_once()

    assert result["rest_fallbacks"] == 1
    assert result["rest_fallback_errors"] == 0
    assert result["exit_all"] == 1
    evaluation = record.call_args.args[1]
    assert evaluation["reason"] == "hard_stop"
    assert evaluation["market_data_source"] == "schwab_rest"
    assert "redis down" in evaluation["stream_snapshot_error"]


def test_worker_blocks_blind_exit_when_stream_and_rest_both_fail(monkeypatch):
    monkeypatch.setattr(
        position_exit_worker,
        "list_positions_for_protection",
        MagicMock(return_value=[{"position": _position(), "protective_intent": None}]),
    )
    monkeypatch.setattr(
        position_exit_worker,
        "get_trading_access_token",
        MagicMock(side_effect=RuntimeError("network down")),
    )
    worker = position_exit_worker.PositionExitWorker()
    worker.apply_stops = False
    monkeypatch.setattr(worker, "_snapshot", MagicMock(side_effect=RuntimeError("redis down")))
    record = MagicMock()
    monkeypatch.setattr(worker, "_record_actionable", record)

    result = worker.run_once()

    assert result["rest_fallbacks"] == 0
    assert result["rest_fallback_errors"] == 1
    assert result["blocked"] == 1
    evaluation = record.call_args.args[1]
    assert evaluation["reason"] == "invalid_position_state"
    assert evaluation["market_data_source"] == "stream"
    assert "network down" in evaluation["rest_fallback_error"]


def test_database_interruption_sets_error_heartbeat_without_broker_action(monkeypatch):
    monkeypatch.setattr(
        position_exit_worker,
        "list_positions_for_protection",
        MagicMock(side_effect=ConnectionError("postgres unavailable")),
    )
    order_client = MagicMock()
    monkeypatch.setattr(
        position_exit_worker, "SchwabOrderClient", MagicMock(return_value=order_client)
    )
    worker = position_exit_worker.PositionExitWorker()
    heartbeat = MagicMock()
    monkeypatch.setattr(worker, "_heartbeat", heartbeat)
    monkeypatch.setattr(
        position_exit_worker.time,
        "sleep",
        MagicMock(side_effect=_StopWorkerLoop()),
    )

    try:
        worker.run_forever()
    except _StopWorkerLoop:
        pass

    assert heartbeat.call_args.args[0] == "error"
    assert "postgres unavailable" in heartbeat.call_args.kwargs["error"]
    order_client.place_order.assert_not_called()
