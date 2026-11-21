from datetime import datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from services import historical_outcome_evaluator as evaluator


EASTERN = ZoneInfo("America/New_York")


def _ts(hour: int, minute: int) -> int:
    return int(datetime(2026, 9, 17, hour, minute, tzinfo=EASTERN).timestamp())


def _candidate(ts: int | None = None):
    return {"id": "candidate-1", "symbol": "NVDA", "ts": ts or _ts(9, 29), "payload": {}}


def _candle(index: int, *, received_ts: int = 2_000_000_000):
    event_ts = _ts(9, 30) + index * 60
    price = 10 + index / 100
    return {
        "observation_key": f"candle-{index}",
        "event_ts": event_ts,
        "received_ts": received_ts,
        "open": price,
        "high": price + 0.2,
        "low": price - 0.1,
        "close": price + 0.1,
        "volume": 100,
    }


def test_evaluate_candidate_builds_complete_regular_session_horizons():
    result = evaluator.evaluate_candidate(
        _candidate(), [_candle(index) for index in range(390)]
    )

    assert result["status"] == "complete"
    assert result["entry_event_ts"] == _ts(9, 30)
    assert result["entry_price"] == 10.0
    assert set(result["horizons"]) == {"5", "15", "60", "390"}
    assert result["horizons"]["5"]["ending_observation_key"] == "candle-4"
    assert result["horizons"]["5"]["return_pct"] > 0
    assert result["retrospective"] is True
    assert result["live_received"] is False
    assert result["live_activation_eligible"] is False


def test_evaluate_candidate_excludes_same_time_and_extended_hours_candles():
    signal_ts = _ts(9, 30)
    premarket = _candle(0)
    premarket["event_ts"] = _ts(8, 0)
    same_time = _candle(0)
    later = _candle(1)

    result = evaluator.evaluate_candidate(
        _candidate(signal_ts), [premarket, same_time, later], horizons=(1,)
    )

    assert result["status"] == "complete"
    assert result["entry_event_ts"] == _ts(9, 31)
    assert result["entry_observation_key"] == "candle-1"


def test_evaluate_candidate_labels_partial_and_missing_coverage():
    partial = evaluator.evaluate_candidate(
        _candidate(), [_candle(index) for index in range(20)]
    )
    missing = evaluator.evaluate_candidate(_candidate(), [], horizons=(5,))

    assert partial["status"] == "partial"
    assert set(partial["horizons"]) == {"5", "15"}
    assert partial["reason"] == "incomplete_forward_window"
    assert missing["status"] == "insufficient"
    assert missing["reason"] == "no_later_regular_candle"


def test_record_outcome_is_idempotent(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = ("outcome-key",)
    connection = MagicMock()
    connection.__enter__.return_value.execute.return_value = cursor
    monkeypatch.setattr(
        evaluator.psycopg, "connect", MagicMock(return_value=connection)
    )
    outcome = evaluator.evaluate_candidate(
        _candidate(), [_candle(index) for index in range(5)], horizons=(5,)
    )

    assert evaluator.record_outcome(outcome) is True
    sql = connection.__enter__.return_value.execute.call_args.args[0]
    params = connection.__enter__.return_value.execute.call_args.args[1]
    assert "ON CONFLICT (candidate_id, evaluator_version) DO NOTHING" in sql
    assert params["outcome_key"] == evaluator._outcome_key("candidate-1")
