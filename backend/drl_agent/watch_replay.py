"""Causal scanner watch-list replay; no fills, models, sleeps, or broker calls."""
from dataclasses import asdict, dataclass
import hashlib
import math

from drl_agent.dataset import _normalize, encode_dataset

VERSION = "drl-watch-replay-v2"


@dataclass(frozen=True)
class WatchConfig:
    minimum_price: float
    maximum_price: float
    minimum_rvol: float
    maximum_float: float
    capacity: int
    snapshot_max_age_seconds: int = 900
    watch_window_seconds: int = 3600

    def __post_init__(self):
        if (any(isinstance(v, bool) or not math.isfinite(float(v)) for v in asdict(self).values())
                or not 0 < self.minimum_price <= self.maximum_price
                or self.minimum_rvol < 0 or self.maximum_float <= 0
                or not isinstance(self.capacity, int) or not 1 <= self.capacity <= 1000
                or not isinstance(self.snapshot_max_age_seconds, int)
                or not 1 <= self.snapshot_max_age_seconds <= 86400
                or not isinstance(self.watch_window_seconds, int)
                or not 1 <= self.watch_window_seconds <= 604800):
            raise ValueError("Invalid watch configuration")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _candidate(row, now, config):
    if now * 1000 - row["event_ms"] > config.snapshot_max_age_seconds * 1000:
        return None, "scanner_stale"
    payload = row["record"].get("payload") or {}
    inputs = payload.get("watch_inputs") or {}
    if inputs.get("version") != "scanner-watch-inputs-v1":
        return None, "watch_inputs_missing"
    observed = _number(inputs.get("observed_at"))
    if observed is None or observed * 1000 != row["event_ms"]:
        return None, "watch_inputs_time_invalid"
    price, rvol, shares = [_number(inputs.get(k)) for k in ("price", "rvol", "float_shares")]
    if price is None or price <= 0:
        return None, "price_unknown"
    if not config.minimum_price <= price <= config.maximum_price:
        return None, "price_out_of_range"
    blockers = []
    if rvol is None or rvol < 0:
        blockers.append("rvol_unknown")
    elif rvol < config.minimum_rvol:
        blockers.append("rvol_below_threshold")
    if shares is None or shares <= 0:
        blockers.append("float_unknown")
    elif shares > config.maximum_float:
        blockers.append("float_above_threshold")
    hype = (payload.get("semantic_signal") or {}).get("hype") or {}
    ids = hype.get("evidence_article_ids")
    if not isinstance(ids, list) or not any(isinstance(v, str) and v.strip() for v in ids):
        return None, "news_evidence_missing"
    evidence = sorted({v.strip() for v in ids if isinstance(v, str) and v.strip()})
    return {"symbol": row["record"]["symbol"].upper(), "snapshot_id": row["id"],
            "snapshot_ts": row["event_ms"] // 1000, "price": price, "rvol": rvol,
            "float_shares": shares, "evidence_article_ids": evidence,
            "momentum_triggered": rvol is not None and rvol >= config.minimum_rvol,
            "entry_block_reasons": blockers,
            "requires_technical_and_risk_evaluation": True,
            "entry_eligible": False}, None


def replay_watch_windows(rows, *, start_ts, end_ts, step_seconds, config):
    """Reevaluate all known candidates on a simulated clock, retaining failures.

    Caller provides explicit research filters; never substitute today's company
    metadata for an unavailable historical measurement. No outcome field is read.
    """
    if (any(isinstance(v, bool) or not isinstance(v, int) for v in (start_ts, end_ts, step_seconds))
            or start_ts <= 0 or end_ts < start_ts or step_seconds < 1
            or (end_ts - start_ts) // step_seconds + 1 > 100000 or len(rows) > 100000):
        raise ValueError("Replay exceeds bounded input/clock limits")
    prepared, excluded = [], {}
    for raw in rows:
        try:
            if raw.get("source") != "scanner_shadow" or not str(raw.get("symbol") or "").strip():
                raise ValueError("Unsupported scanner row")
            row = _normalize(raw, scanner=True)
            if row["available_ms"] <= end_ts * 1000:
                prepared.append(row)
        except (KeyError, TypeError, ValueError, OverflowError):
            excluded["invalid_scanner_record"] = excluded.get("invalid_scanner_record", 0) + 1
    prepared.sort(key=lambda r: (r["available_ms"], r["received_ms"], r["id"]))
    symbols = {r["record"]["symbol"].upper() for r in prepared}
    if len(symbols) * ((end_ts - start_ts) // step_seconds + 1) > 250000:
        raise ValueError("Replay output budget exceeded; export a shorter time segment")
    latest, active, windows = {}, {}, {}
    frames, cursor = [], 0
    for now in range(start_ts, end_ts + 1, step_seconds):
        while cursor < len(prepared) and prepared[cursor]["available_ms"] <= now * 1000:
            row = prepared[cursor]
            symbol = row["record"]["symbol"].upper()
            old = latest.get(symbol)
            if old is None or (row["event_ms"], row["received_ms"], row["id"]) > (
                    old["event_ms"], old["received_ms"], old["id"]):
                latest[symbol] = row
            cursor += 1
        candidates, rejected = [], {}
        for symbol, row in sorted(latest.items()):
            candidate, reason = _candidate(row, now, config)
            if reason:
                rejected[symbol] = reason
                continue
            identity = tuple(candidate["evidence_article_ids"])
            previous = windows.get((symbol, identity))
            if previous is not None and now >= previous + config.watch_window_seconds:
                rejected[symbol] = "watch_window_expired"
                continue
            candidate["watch_started_ts"] = previous if previous is not None else now
            candidates.append(candidate)
        candidates.sort(key=lambda c: (not c["momentum_triggered"],
                                      -(c["rvol"] if c["rvol"] is not None else -1), c["symbol"]))
        selected = candidates[:config.capacity]
        for candidate in selected:
            windows[(candidate["symbol"], tuple(candidate["evidence_article_ids"]))] = candidate["watch_started_ts"]
        rejected.update({c["symbol"]: "watch_capacity" for c in candidates[config.capacity:]})
        new_active = {c["symbol"]: c for c in selected}
        frames.append({"decision_ts": now, "watching": selected,
                       "entered_watch": sorted(new_active.keys() - active.keys()),
                       "left_watch": [{"symbol": s, "reason": rejected[s]}
                                      for s in sorted(active.keys() - new_active.keys())],
                       "rejected": rejected})
        active = new_active
    result = {"manifest": {"version": VERSION, "config": asdict(config),
                            "start_ts": start_ts, "end_ts": end_ts, "step_seconds": step_seconds,
                            "frame_count": len(frames), "input_rows": len(rows), "excluded": excluded,
                            "earliest_available_ts": math.ceil(prepared[0]["available_ms"] / 1000) if prepared else None,
                            "latest_available_ts": math.ceil(prepared[-1]["available_ms"] / 1000) if prepared else None,
                            "training_ready": False, "broker_calls": 0,
                            "execution_supported": False}, "frames": frames}
    result["manifest"]["replay_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result
