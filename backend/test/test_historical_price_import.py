from unittest.mock import MagicMock

from services import historical_price_import


def test_price_status_database_reads_have_time_limits(monkeypatch):
    connection = MagicMock()
    cursor = connection.__enter__.return_value.execute.return_value
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = {"count": 0}
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(historical_price_import.psycopg, "connect", connect)
    assert historical_price_import.status()["coverage"]["count"] == 0
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert "statement_timeout=5000" in connect.call_args.kwargs["options"]


def test_task_summary_does_not_scan_market_observations(monkeypatch):
    connection = MagicMock()
    execute = connection.__enter__.return_value.execute
    execute.return_value.fetchall.return_value = [{"status": "complete", "count": 2, "candles": 100}]
    monkeypatch.setattr(historical_price_import.psycopg, "connect", MagicMock(return_value=connection))
    result = historical_price_import.status(include_coverage=False)
    assert execute.call_count == 1
    assert "market_observations" not in execute.call_args.args[0]
    assert result["coverage"] == {}
    assert result["coverage_status"] == "not_requested"
    assert result["task_candles"] == 100


def _task(**updates):
    task = {
        "import_key": "task-1",
        "symbol": "NVDA",
        "start_ts": 1_000,
        "end_ts": 2_000,
        "frequency_minutes": 1,
    }
    task.update(updates)
    return task


def test_normalize_candles_preserves_event_time_and_marks_retroactive_receipt():
    rows = historical_price_import.normalize_candles(
        _task(),
        {
            "candles": [{
                "datetime": 1_200_000,
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1234,
            }]
        },
        received_ts=9_000,
    )

    assert rows == [{
        "symbol": "NVDA",
        "observation_type": "historical_minute_candle",
        "source": "schwab_price_history",
        "event_ts": 1_200,
        "received_ts": 9_000,
        "price": 10.2,
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "volume": 1234.0,
        "payload": {
            "symbol": "NVDA",
            "datetime": 1_200_000,
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 1234.0,
            "frequency_minutes": 1,
            "import_version": "schwab-price-history-v1",
            "imported_ts": 9_000,
        },
    }]


def test_normalize_candles_rejects_out_of_range_and_invalid_ohlc():
    rows = historical_price_import.normalize_candles(
        _task(),
        {"candles": [
            {"datetime": 999_000, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
            {"datetime": 1_200_000, "open": 10, "high": 9, "low": 8, "close": 10, "volume": 1},
            {"datetime": 1_300_000, "open": 10, "high": 11, "low": 10.5, "close": 10, "volume": 1},
            {"datetime": 1_400_000, "open": "bad", "high": 11, "low": 9, "close": 10, "volume": 1},
        ]},
        received_ts=9_000,
    )

    assert rows == []


def test_store_result_writes_only_retrospective_observation_type(monkeypatch):
    record = MagicMock(return_value=1)
    execute = MagicMock()
    connection = MagicMock()
    connection.__enter__.return_value.execute = execute
    monkeypatch.setattr(historical_price_import.persistent_store, "record_market_observations", record)
    monkeypatch.setattr(
        historical_price_import.psycopg,
        "connect",
        MagicMock(return_value=connection),
    )

    result = historical_price_import.store_result(
        _task(),
        {"candles": [{
            "datetime": 1_200_000, "open": 10, "high": 11,
            "low": 9, "close": 10.5, "volume": 100,
        }]},
        received_ts=9_000,
    )

    observations = record.call_args.args[0]
    assert result == {"candles": 1, "stored": 1}
    assert observations[0]["observation_type"] == "historical_minute_candle"
    assert observations[0]["received_ts"] == 9_000
    assert record.call_args.kwargs == {"ensure": False}
    assert "status = 'complete'" in execute.call_args.args[0]


def test_import_key_is_stable_and_range_specific():
    first = historical_price_import._import_key("nvda", 100, 200, 1)
    assert first == historical_price_import._import_key("NVDA", 100, 200, 1)
    assert first != historical_price_import._import_key("NVDA", 100, 201, 1)


def test_recover_interrupted_imports_returns_running_tasks_to_retry(monkeypatch):
    cursor = MagicMock(rowcount=2)
    connection = MagicMock()
    connection.__enter__.return_value.execute.return_value = cursor
    monkeypatch.setattr(
        historical_price_import.psycopg,
        "connect",
        MagicMock(return_value=connection),
    )

    assert historical_price_import.recover_interrupted_imports() == 2
    sql = connection.__enter__.return_value.execute.call_args.args[0]
    assert "status = 'retry'" in sql
    assert "WHERE status = 'running'" in sql
