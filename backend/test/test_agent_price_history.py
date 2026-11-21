from datetime import datetime, timezone

import pandas as pd
import pytest

from services.agent_price_history import validated_agent_daily_frame


NOW = datetime(2026, 9, 24, 15, tzinfo=timezone.utc)


def _frame():
    return pd.DataFrame({
        "t": pd.date_range(end="2026-09-23T05:00:00Z", periods=60, freq="B"),
        "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000,
    })


def test_preserves_dates_and_ohlcv_without_mutating_input():
    frame = _frame()
    result = validated_agent_daily_frame(frame, now=NOW)
    assert list(result.columns) == ["t", "Open", "High", "Low", "Close", "Volume"]
    assert result["t"].equals(frame["t"])
    assert result["Close"].iloc[-1] == 10.5
    assert "c" in frame


@pytest.mark.parametrize("frame", [None, pd.DataFrame(), _frame().iloc[:39], _frame().drop(columns="t")])
def test_missing_or_short_history_is_not_replaced(frame):
    with pytest.raises(ValueError):
        validated_agent_daily_frame(frame, now=NOW)


@pytest.mark.parametrize("column,value", [
    ("o", float("nan")), ("v", float("inf")), ("v", -1),
    ("c", 0), ("h", 9.5), ("l", 10.7), ("c", "invalid"),
])
def test_invalid_candles_are_rejected(column, value):
    frame = _frame().astype({column: object})
    frame.loc[59, column] = value
    with pytest.raises(ValueError, match="invalid_values"):
        validated_agent_daily_frame(frame, now=NOW)


@pytest.mark.parametrize("mode", ["duplicate", "future", "stale", "unsorted", "missing"])
def test_invalid_dates_are_rejected(mode):
    frame = _frame()
    if mode == "duplicate":
        frame.loc[59, "t"] = frame.loc[58, "t"]
    elif mode == "future":
        frame.loc[59, "t"] = pd.Timestamp("2026-09-25T05:00:00Z")
    elif mode == "stale":
        frame["t"] -= pd.Timedelta(days=10)
    elif mode == "unsorted":
        frame = frame.iloc[::-1]
    else:
        frame.loc[59, "t"] = pd.NaT
    with pytest.raises(ValueError):
        validated_agent_daily_frame(frame, now=NOW)
