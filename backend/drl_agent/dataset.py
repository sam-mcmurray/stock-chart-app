"""Offline, causal observation sequences. No model, broker, or network calls."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from typing import Any

VERSION = "drl-observations-v1"
TYPES = ("levelone_quote", "level2_book", "minute_candle")


def _positive(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean timestamp")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Missing or invalid timestamp")
    return math.ceil(number)


def _created_ms(value: Any) -> int:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Scanner creation time must include timezone")
    return _positive(value.timestamp() * 1000)


def _normalize(row: dict, *, scanner: bool) -> dict:
    payload = row.get("payload") or {}
    if scanner:
        event = _positive(row["ts"]) * 1000
        received = _created_ms(row["created_at"])
        hype_time = ((payload.get("semantic_signal") or {}).get("hype") or {}).get("as_of_ts")
        available = max(event, received, _positive(hype_time) * 1000 if hype_time else event)
        kind, identity, service = "scanner", row["id"], "scanner_shadow"
    else:
        kind, identity = row["observation_type"], row["observation_key"]
        event = _positive(row["event_ts"]) * 1000
        # Second-only receipt records are available at the end of that second.
        received = max(_positive(row["received_ts"]) * 1000,
                       _positive(payload["received_at_ms"]) if payload.get("received_at_ms")
                       else _positive(row["received_ts"]) * 1000 + 999)
        event_fields = ("quote_time_ms", "trade_time_ms", "bid_time_ms", "ask_time_ms")
        if kind == "levelone_quote":
            event = max([event] + [_positive(payload[k]) for k in event_fields if payload.get(k)])
        elif kind == "level2_book":
            event = max(event, _positive(payload["market_time_ms"]) if payload.get("market_time_ms") else event)
        elif kind == "minute_candle":
            event = max(event, _positive(payload["chart_time_ms"]) if payload.get("chart_time_ms") else event)
        available = max(received, event + (60_000 if kind == "minute_candle" else 0))
        service = payload.get("service") or kind
    if not identity:
        raise ValueError("Missing source identity")
    return {"kind": kind, "id": str(identity), "event_ms": event,
            "received_ms": received, "available_ms": available,
            "service": str(service), "record": deepcopy(row)}


def build_dataset(
    symbol: str, *, start_ts: int, end_ts: int,
    scanner_rows: list[dict], market_rows: list[dict],
    step_seconds: int = 10, lookback_seconds: int = 900, history_limit: int = 120,
) -> dict:
    symbol = symbol.strip().upper()
    if not symbol or start_ts <= 0 or not start_ts <= end_ts <= start_ts + 86_400:
        raise ValueError("Provide a symbol and at most one day of decision times")
    if step_seconds < 1 or (end_ts - start_ts) // step_seconds + 1 > 10_000:
        raise ValueError("Decision grid exceeds 10,000 states")
    if not 60 <= lookback_seconds <= 86_400 or not 1 <= history_limit <= 1000:
        raise ValueError("Invalid history bounds")
    if ((end_ts - start_ts) // step_seconds + 1) * (3 * history_limit + 1) > 250_000:
        raise ValueError("Output history budget exceeded; shorten the window or history")
    prepared = []
    exclusions: dict[str, int] = {}
    for scanner, rows in ((True, scanner_rows), (False, market_rows)):
        for row in rows:
            reason = None
            if str(row.get("symbol", "")).upper() != symbol:
                reason = "other_symbol"
            elif row.get("source") != ("scanner_shadow" if scanner else "schwab_stream"):
                reason = "unsupported_source"
            elif not scanner and row.get("observation_type") not in TYPES:
                reason = "unsupported_type"
            else:
                try:
                    prepared.append(_normalize(row, scanner=scanner))
                except (KeyError, TypeError, ValueError, OverflowError):
                    reason = "invalid_record"
            if reason:
                exclusions[reason] = exclusions.get(reason, 0) + 1
    prepared.sort(key=lambda r: (r["available_ms"], r["received_ms"], r["id"]))
    active: dict[tuple, dict] = {}
    cursor = 0
    states = []
    for decision_ts in range(start_ts, end_ts + 1, step_seconds):
        cutoff = decision_ts * 1000
        while cursor < len(prepared) and prepared[cursor]["available_ms"] <= cutoff:
            item = prepared[cursor]
            key = (item["kind"], item["service"], item["event_ms"])
            old = active.get(key)
            if old is None or (item["received_ms"], item["id"]) > (old["received_ms"], old["id"]):
                active[key] = item
            cursor += 1
        active = {k: v for k, v in active.items() if v["event_ms"] >= cutoff - lookback_seconds * 1000}
        state = {"decision_ts": decision_ts, "symbol": symbol, "inputs": {}, "coverage": {}}
        for kind in ("scanner", *TYPES):
            rows = sorted((r for r in active.values() if r["kind"] == kind),
                          key=lambda r: (r["event_ms"], r["received_ms"], r["id"]))
            latest = rows[-1] if rows else None
            selected = rows[-(1 if kind == "scanner" else history_limit):]
            # Raw snapshots are evidence, not a policy observation vector. The
            # later feature encoder must whitelist inputs and exclude rewards.
            state["inputs"][kind] = [deepcopy(r) for r in selected]
            state["coverage"][kind] = {
                "present": bool(rows), "count": len(selected),
                "history_truncated": len(rows) > len(selected) if kind != "scanner" else False,
                "event_age_ms": cutoff - latest["event_ms"] if latest else None,
                "receive_age_ms": cutoff - latest["received_ms"] if latest else None,
            }
        states.append(state)
    manifest = {
        "version": VERSION, "symbol": symbol, "start_ts": start_ts, "end_ts": end_ts,
        "step_seconds": step_seconds, "lookback_seconds": lookback_seconds,
        "history_limit": history_limit, "state_count": len(states),
        "input_scanner_count": len(scanner_rows), "input_market_count": len(market_rows),
        "excluded": exclusions, "broker_calls": 0, "training_ready": False,
        "coverage_states": {kind: sum(s["coverage"][kind]["present"] for s in states)
                            for kind in ("scanner", *TYPES)},
    }
    result = {"manifest": manifest, "states": states}
    manifest["dataset_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def encode_dataset(dataset: dict) -> str:
    def default(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Unsupported dataset value: {type(value).__name__}")
    return json.dumps(dataset, sort_keys=True, separators=(",", ":"), default=default, allow_nan=False)
