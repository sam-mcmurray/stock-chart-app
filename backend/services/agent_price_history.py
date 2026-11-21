"""Strict dated daily bars for agent inference; no synthetic fallback."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


def validated_agent_daily_frame(frame, *, now=None):
    if frame is None or len(frame) < 40:
        raise ValueError("daily_history_insufficient")
    required = ["t", "o", "h", "l", "c", "v"]
    if not set(required).issubset(frame.columns):
        raise ValueError("daily_history_columns_missing")
    result = frame[required].copy().reset_index(drop=True)
    stamps = pd.to_datetime(result["t"], utc=True, errors="coerce")
    if stamps.isna().any() or not stamps.is_monotonic_increasing:
        raise ValueError("daily_history_invalid_dates")
    days = stamps.dt.tz_convert("America/New_York").dt.date
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York")).date()
    if days.duplicated().any() or (days > today).any():
        raise ValueError("daily_history_invalid_dates")
    if (today - days.iloc[-1]).days > 4:
        raise ValueError("daily_history_stale")
    values = result[["o", "h", "l", "c", "v"]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError("daily_history_invalid_values")
    if ((values[["o", "h", "l", "c"]] <= 0).any().any()
            or (values["v"] < 0).any()
            or (values["h"] < values[["o", "l", "c"]].max(axis=1)).any()
            or (values["l"] > values[["o", "h", "c"]].min(axis=1)).any()):
        raise ValueError("daily_history_invalid_values")
    result["t"] = stamps
    result[values.columns] = values
    return result.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
