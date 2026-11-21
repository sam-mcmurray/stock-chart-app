"""Causal bar-derived research features; not live Scanner eligibility."""
import argparse
from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify
from drl_agent.trade_volume_evidence import volume_minutes


def load_warmup(archive, joined, *, volume_window=90):
    from drl_agent.hf_replay_export import export_archive
    _verify(joined, "historical-news-price-join-v1", "join_sha256")
    if type(volume_window) is not int or not 1 <= volume_window <= 252:
        raise ValueError("Invalid volume window")
    end = pd.Timestamp(joined["candles"][0]["start_ts"], unit="s", tz="UTC")
    day = end.tz_convert("America/New_York").date()
    first = day - timedelta(days=volume_window * 2 + 30)
    cal = xcals.get_calendar("XNYS", start=first, end=day + timedelta(days=7))
    prior = cal.sessions_in_range(str(first), str(day - timedelta(days=1)))
    start = cal.session_open(prior[-volume_window])
    chunks = []
    while start < end:
        stop = min(end, start + pd.Timedelta(days=31))
        try:
            chunks.append(export_archive(archive, start=start, end=stop))
        except ValueError as exc:
            if str(exc) != "No valid completed candles in requested window":
                raise
        start = stop
    return chunks


def build_features(joined, *, volume_window=90, minimum_sessions=20, warmup=None, trade_evidence=()):
    _verify(joined, "historical-news-price-join-v1", "join_sha256")
    if (type(volume_window) is not int or type(minimum_sessions) is not int
            or not 1 <= minimum_sessions <= volume_window <= 252):
        raise ValueError("Invalid RVOL history bounds")
    if warmup:
        history = []
        hashes = []
        for chunk in warmup:
            if chunk.get("version") not in {"hf-historical-replay-inputs-v1", "alpaca-historical-replay-inputs-v1"}:
                raise ValueError("Unsupported warmup source")
            _verify(chunk, chunk["version"], "export_sha256")
            if (chunk["symbol"] != joined["symbol"]
                    or chunk["source_file_sha256"] != joined["source_file_sha256"]
                    or {c["source"] for c in chunk["candles"]} != {c["source"] for c in joined["candles"]}):
                raise ValueError("Warmup must use the same symbol and archive")
            history.extend(chunk["candles"])
            hashes.append(chunk["export_sha256"])
        history.sort(key=lambda c: c["start_ts"])
        if any(c["end_ts"] > joined["candles"][0]["start_ts"] for c in history):
            raise ValueError("Warmup overlaps replay or contains future candles")
        expanded = deepcopy(joined)
        expanded["candles"] = history + expanded["candles"]
        if all("requested_start_ts" in chunk for chunk in warmup) and "requested_start_ts" in joined:
            expanded["requested_start_ts"] = min(joined["requested_start_ts"], *(c["requested_start_ts"] for c in warmup))
        else:
            expanded.pop("requested_start_ts", None)
        expanded["states"] = [{"candle_id": c["id"], "decision_ts": c["end_ts"],
                               "news_candidate_ids": [], "news_watch_active": False}
                              for c in history] + expanded["states"]
        expanded.pop("join_sha256")
        expanded["join_sha256"] = hashlib.sha256(encode_dataset(expanded).encode()).hexdigest()
        result = build_features(expanded, volume_window=volume_window, minimum_sessions=minimum_sessions,
                                trade_evidence=trade_evidence)
        result["states"] = result["states"][len(history):]
        result["source_join_sha256"] = joined["join_sha256"]
        result["warmup"] = {"candles": len(history), "export_hashes": hashes,
                            "first_start_ts": history[0]["start_ts"] if history else None,
                            "last_end_ts": history[-1]["end_ts"] if history else None}
        result["coverage"] = _coverage(result["states"])
        result.pop("features_sha256")
        result["features_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
        return result
    candles, states = joined["candles"], joined["states"]
    if not candles or len(candles) != len(states) or len(candles) > 100000:
        raise ValueError("Invalid candle/state alignment")
    if len({c["source"] for c in candles}) != 1:
        raise ValueError("Do not mix volume sources")
    supplements = volume_minutes(trade_evidence, joined["symbol"]) if trade_evidence else {}
    if supplements and ({c["source"] for c in candles} != {"alpaca_sip"}
                        or set(supplements) & {c["start_ts"] for c in candles}):
        raise ValueError("Volume evidence must fill absent SIP minutes only")
    requested_start = joined.get("requested_start_ts")
    if requested_start is not None and (type(requested_start) is not int or requested_start <= 0 or requested_start > candles[0]["start_ts"]):
        raise ValueError("Invalid requested coverage start")
    first = datetime.fromtimestamp(requested_start or candles[0]["start_ts"], timezone.utc).date()
    last = datetime.fromtimestamp(candles[-1]["end_ts"], timezone.utc).date()
    cal = xcals.get_calendar("XNYS", start=first - timedelta(days=7), end=last + timedelta(days=7))
    sessions = [(int(cal.session_open(s).timestamp()), int(cal.session_close(s).timestamp()))
                for s in cal.sessions_in_range(str(first), str(last))]
    excluded_boundary = []
    session_coverage = []
    closes = pd.Series([c["close"] for c in candles], dtype=float)
    macd = closes.ewm(span=12, adjust=False).mean() - closes.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    baseline = deque(maxlen=volume_window)
    session_index, current, previous_end, contiguous = 0, None, 0, 0
    volume, weighted, count, complete_prefix = 0., 0., 0, False
    output = []
    for index, (candle, state) in enumerate(zip(candles, states)):
        start, end = candle["start_ts"], candle["end_ts"]
        if (candle["id"] != state["candle_id"] or state["decision_ts"] != end
                or start < previous_end or end != start + 60 or start % 60):
            raise ValueError("Invalid or nonchronological minute states")
        while session_index < len(sessions) and start >= sessions[session_index][1]:
            session_open, session_close = sessions[session_index]
            observed = count if current == sessions[session_index] else 0
            expected = (session_close - session_open) // 60
            boundary = requested_start is not None and session_open < requested_start
            additions = [r for t, r in supplements.items() if session_open <= t and r["available_ts"] <= session_close]
            volume_count = observed + len(additions)
            completed_volume = (volume if observed else 0) + sum(r["volume"] for r in additions)
            session_coverage.append({"open_ts": session_open, "close_ts": session_close,
                "expected_minutes": expected, "observed_minutes": observed,
                "missing_minutes": expected - observed, "partial_request_boundary": boundary,
                "trade_verified_minutes": len(additions), "unresolved_volume_minutes": expected - volume_count})
            if boundary:
                excluded_boundary.append(session_open)
                session_index += 1
                current = None
                continue
            if additions:
                baseline.append((completed_volume, volume_count, expected, volume_count == expected))
            elif current == sessions[session_index]:
                expected = (current[1] - current[0]) // 60
                if complete_prefix and count == expected:
                    baseline.append((volume, count, expected, True))
                else:
                    baseline.append((volume, count, expected, False))
            else:
                expected = (sessions[session_index][1] - sessions[session_index][0]) // 60
                baseline.append((None, 0, expected, False))
            session_index += 1
            current = None
        session = sessions[session_index] if session_index < len(sessions) else None
        regular = bool(session and session[0] <= start and end <= session[1])
        if regular:
            if current != session:
                current = session
                volume, weighted, count = 0., 0., 0
                complete_prefix = start == session[0]
            else:
                complete_prefix = complete_prefix and start == previous_end
            count += 1
            volume += candle["volume"]
            weighted += (candle["high"] + candle["low"] + candle["close"]) / 3 * candle["volume"]
        contiguous = contiguous + 1 if regular and (index == 0 or start == previous_end) else 1 if regular else 0
        previous_end = end
        history = [v for v, _, _, complete in baseline if complete]
        history_ok = len(history) >= minimum_sessions and len(history) == len(baseline)
        average = sum(history) / len(history) if history else None
        rvol = volume / average if regular and complete_prefix and history_ok and average and average > 0 else None
        observed_history = [v for v, _, _, _ in baseline if v is not None]
        observed_average = sum(observed_history) / len(observed_history) if observed_history else None
        observed_ready = (regular and len(observed_history) >= minimum_sessions
                          and len(observed_history) == len(baseline) and observed_average and observed_average > 0)
        observed_rvol = volume / observed_average if observed_ready else None
        expected_prior = sum(expected for _, _, expected, _ in baseline)
        observed_prior = sum(observed for _, observed, _, _ in baseline)
        expected_prefix = (end - session[0]) // 60 if regular else 0
        strict_reason = (None if rvol is not None else "outside_regular_session" if not regular
                         else "incomplete_current_session" if not complete_prefix
                         else "insufficient_prior_sessions" if len(baseline) < minimum_sessions
                         else "incomplete_prior_sessions" if len(history) != len(baseline)
                         else "zero_prior_volume")
        macd_ok = regular and contiguous >= 40
        output.append({"decision_ts": end, "symbol": joined["symbol"], "candle_id": candle["id"],
                       "news_candidate_ids": state["news_candidate_ids"],
                       "news_watch_active": state["news_watch_active"], "regular_session": regular,
                       "observed_adjusted_close": candle["close"] if candle.get("price_basis") != "nominal" else None,
                       "price_basis": candle.get("price_basis", "adjusted"), "volume_source": candle["source"],
                       "observed_session_volume": volume if regular else None,
                       "session_prefix_complete": complete_prefix if regular else False,
                       "prior_session_count": len(baseline), "complete_prior_session_count": len(history),
                       "rvol": rvol, "rvol_valid": rvol is not None,
                       "rvol_basis": "consolidated_bar_and_trade_volume_proxy" if supplements else "consolidated_bar_proxy",
                       "rvol_unavailable_reason": strict_reason,
                       "observed_volume_rvol": observed_rvol,
                       "observed_volume_rvol_valid": observed_rvol is not None,
                       "observed_prior_session_count": len(observed_history),
                       "prior_minute_coverage": observed_prior / expected_prior if expected_prior else None,
                       "current_minute_coverage": count / expected_prefix if expected_prefix else None,
                       "missing_prior_minutes": expected_prior - observed_prior,
                       "missing_current_minutes": expected_prefix - count if regular else None,
                       "bar_vwap_estimate": weighted / volume if regular and complete_prefix and volume > 0 else None,
                       "minute_macd": float(macd.iloc[index]) if macd_ok else None,
                       "minute_macd_signal": float(signal.iloc[index]) if macd_ok else None,
                       "minute_macd_histogram": float(macd.iloc[index] - signal.iloc[index]) if macd_ok else None,
                       "historical_nominal_price": candle["close"] if candle.get("price_basis") == "nominal" else None,
                       "float_shares": None,
                       "entry_eligible": False, "scanner_eligibility": "unknown"})
    result = {"version": "historical-bar-features-v2", "source_join_sha256": joined["join_sha256"],
              "symbol": joined["symbol"], "volume_window": volume_window, "minimum_sessions": minimum_sessions,
              "rvol_definition": "observed_regular_session_volume / mean_prior_complete_regular_session_volumes",
              "observed_volume_rvol_definition": "observed_current_volume / mean_observed_prior_session_volumes",
              "observed_volume_rvol_policy": "research_only_partial_volume_not_zero_filled_not_scanner_admission",
              "macd_definition": "one_minute_12_26_9_ewm_adjust_false_not_daily_scanner_macd",
              "vwap_definition": "cumulative_typical_bar_price_times_volume_not_trade_tick_vwap",
              "retrospective": True, "training_ready": False, "execution_supported": False,
              "broker_calls": 0, "network_calls": 0, "states": output,
              "prior_session_coverage": session_coverage,
              "excluded_partial_request_sessions": excluded_boundary,
              "volume_gap_evidence": list(supplements.values()),
              "volume_gap_policy": "verified_completed_prior_sessions_only_no_price_or_current_session_fill",
              "coverage": _coverage(output),
              "limitations": joined["limitations"] + ["Missing regular minutes prevent volume completeness certification",
                  "Observed-volume RVOL uses incomplete sums and may be biased by unequal missingness",
                  "IEX-only RVOL must not be treated as consolidated Scanner RVOL"]}
    result["features_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def _coverage(states):
    return {"states": len(states), "rvol_states": sum(s["rvol_valid"] for s in states),
            "observed_volume_rvol_states": sum(s["observed_volume_rvol_valid"] for s in states),
            "strict_rvol_unavailable": dict(Counter(s["rvol_unavailable_reason"] for s in states
                                                    if s["rvol_unavailable_reason"])),
            "vwap_states": sum(s["bar_vwap_estimate"] is not None for s in states),
            "macd_states": sum(s["minute_macd"] is not None for s in states)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--joined", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, help="Local raw archive for prior-session warmup")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    joined = json.loads(args.joined.read_text())
    warmup = load_warmup(args.archive, joined) if args.archive else None
    result = build_features(joined, warmup=warmup)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({k: v for k, v in result.items() if k != "states"}))


if __name__ == "__main__":
    main()
