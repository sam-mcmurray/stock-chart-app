from unittest.mock import MagicMock

from workers import protective_stop_worker


def test_local_mode_never_reads_positions_or_calls_broker(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTE_TRADES", "true")
    monkeypatch.setenv("AGENT_BROKER_PROTECTIVE_STOPS_ENABLED", "false")
    positions = MagicMock()
    monkeypatch.setattr(protective_stop_worker, "list_positions_for_protection", positions)

    worker = protective_stop_worker.ProtectiveStopWorker()
    result = worker.run_once()

    assert worker.execute is False
    assert result["protection_mode"] == "local_trigger"
    assert result["broker_stops_enabled"] is False
    positions.assert_not_called()


def test_broker_stops_require_both_interlocks(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTE_TRADES", "false")
    monkeypatch.setenv("AGENT_BROKER_PROTECTIVE_STOPS_ENABLED", "true")
    assert protective_stop_worker.ProtectiveStopWorker().execute is False

    monkeypatch.setenv("AGENT_EXECUTE_TRADES", "true")
    assert protective_stop_worker.ProtectiveStopWorker().execute is True
