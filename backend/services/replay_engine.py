from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any


REPLAY_VERSION = "news-hype-replay-v2"
ACCEPTANCE_VERSION = "news-hype-acceptance-v1"


@dataclass(frozen=True)
class ReplayConfig:
    initial_equity: float = 10_000.0
    minimum_hype_score: float = 0.25
    risk_per_trade_pct: float = 0.005
    max_position_pct: float = 0.10
    stop_loss_pct: float = 0.02
    entry_latency_seconds: int = 2
    maximum_entry_wait_seconds: int = 180
    maximum_data_delay_seconds: int = 120
    assumed_spread_bps: float = 20.0
    entry_slippage_bps: float = 10.0
    exit_slippage_bps: float = 20.0
    fee_bps: float = 0.0
    entry_volume_participation_pct: float = 0.005
    exit_volume_participation_pct: float = 0.01
    maximum_entry_chase_pct: float = 3.0
    partial_profit_r: float = 1.5
    partial_fraction: float = 0.5
    break_even_r: float = 1.0
    runner_activation_r: float = 2.0
    runner_trail_r: float = 0.75
    minimum_acceptance_trades: int = 30
    minimum_profit_factor: float = 1.2
    minimum_fill_rate: float = 0.8
    maximum_drawdown_pct: float = 5.0


def _validated_config(config: ReplayConfig) -> ReplayConfig:
    values = asdict(config)
    integer_fields = {
        "entry_latency_seconds",
        "maximum_entry_wait_seconds",
        "maximum_data_delay_seconds",
        "minimum_acceptance_trades",
    }
    try:
        normalized = {
            key: int(value) if key in integer_fields else float(value)
            for key, value in values.items()
        }
    except (TypeError, ValueError) as exc:
        raise ValueError("replay config values must be numeric") from exc
    if not all(math.isfinite(float(value)) for value in normalized.values()):
        raise ValueError("replay config values must be finite")
    positive = {
        "initial_equity",
        "risk_per_trade_pct",
        "max_position_pct",
        "stop_loss_pct",
        "maximum_entry_wait_seconds",
        "maximum_data_delay_seconds",
        "partial_profit_r",
        "runner_activation_r",
        "runner_trail_r",
        "minimum_acceptance_trades",
    }
    if any(normalized[key] <= 0 for key in positive):
        raise ValueError("positive replay config values must be greater than zero")
    bounded = {
        "minimum_hype_score",
        "risk_per_trade_pct",
        "max_position_pct",
        "stop_loss_pct",
        "entry_volume_participation_pct",
        "exit_volume_participation_pct",
        "partial_fraction",
        "minimum_fill_rate",
    }
    if any(not 0 <= normalized[key] <= 1 for key in bounded):
        raise ValueError("replay score, risk, participation, and fraction values must be between zero and one")
    nonnegative = {
        "entry_latency_seconds",
        "assumed_spread_bps",
        "entry_slippage_bps",
        "exit_slippage_bps",
        "fee_bps",
        "maximum_entry_chase_pct",
        "break_even_r",
        "minimum_profit_factor",
        "maximum_drawdown_pct",
    }
    if any(normalized[key] < 0 for key in nonnegative):
        raise ValueError("replay latency, costs, and acceptance limits cannot be negative")
    return ReplayConfig(**normalized)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_bars(
    rows: list[dict[str, Any]], *, end_ts: int, maximum_delay: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    audit = {
        "input_market_observations": len(rows),
        "excluded_future_market": 0,
        "excluded_late_revision": 0,
        "excluded_invalid_market": 0,
        "collapsed_revisions": 0,
    }
    revisions: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        event_ts = _integer(row.get("event_ts") or row.get("ts"))
        received_ts = _integer(row.get("received_ts") or row.get("received_at_ts"))
        if event_ts > end_ts or received_ts > end_ts:
            audit["excluded_future_market"] += 1
            continue
        if event_ts <= 0 or received_ts <= 0 or received_ts < event_ts:
            audit["excluded_invalid_market"] += 1
            continue
        if received_ts > event_ts + maximum_delay:
            audit["excluded_late_revision"] += 1
            continue
        open_price = _number(row.get("open"))
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close") or row.get("price"))
        volume = max(0.0, _number(row.get("volume")))
        if (
            min(open_price, high, low, close) <= 0
            or high < max(open_price, close)
            or low > min(open_price, close)
        ):
            audit["excluded_invalid_market"] += 1
            continue
        status = str(row.get("security_status") or "").strip().upper()
        normalized = {
            "event_ts": event_ts,
            "received_ts": received_ts,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "bid": _number(row.get("bid")),
            "ask": _number(row.get("ask")),
            "halted": bool(row.get("halted")) or status in {"HALTED", "FROZEN"},
        }
        revisions.setdefault(event_ts, []).append(normalized)
    bars = []
    for event_ts, candidates in revisions.items():
        candidates.sort(key=lambda row: row["received_ts"])
        bars.append(candidates[-1])
        audit["collapsed_revisions"] += len(candidates) - 1
    bars.sort(key=lambda row: row["event_ts"])
    return bars, audit


def _normalize_quotes(
    rows: list[dict[str, Any]], *, end_ts: int, maximum_delay: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    audit = {
        "input_levelone_quotes": len(rows),
        "excluded_invalid_quotes": 0,
        "excluded_late_quotes": 0,
    }
    quotes = []
    for row in rows:
        payload = row.get("payload") or {}
        event_ts = _integer(row.get("event_ts"))
        received_ts = _integer(row.get("received_ts"))
        bid = _number(row.get("bid") or payload.get("bid"))
        ask = _number(row.get("ask") or payload.get("ask"))
        if (
            event_ts <= 0
            or received_ts <= 0
            or event_ts > end_ts
            or received_ts > end_ts
            or received_ts < event_ts
            or bid <= 0
            or ask < bid
        ):
            audit["excluded_invalid_quotes"] += 1
            continue
        if received_ts > event_ts + maximum_delay:
            audit["excluded_late_quotes"] += 1
            continue
        midpoint = (bid + ask) / 2
        status = str(payload.get("security_status") or "").strip().upper()
        quotes.append({
            "event_ts": event_ts,
            "received_ts": received_ts,
            "bid": bid,
            "ask": ask,
            "midpoint": midpoint,
            "spread_bps": (ask - bid) / midpoint * 10_000,
            "halted": status in {"HALTED", "FROZEN"},
        })
    quotes.sort(key=lambda row: (row["received_ts"], row["event_ts"]))
    return quotes, audit


def _normalize_books(
    rows: list[dict[str, Any]], *, end_ts: int, maximum_delay: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    audit = {
        "input_level2_books": len(rows),
        "excluded_invalid_books": 0,
        "excluded_late_books": 0,
    }
    books = []
    for row in rows:
        payload = row.get("payload") or {}
        event_ts = _integer(row.get("event_ts"))
        received_ts = _integer(row.get("received_ts"))
        if (
            event_ts <= 0
            or received_ts <= 0
            or event_ts > end_ts
            or received_ts > end_ts
            or received_ts < event_ts
        ):
            audit["excluded_invalid_books"] += 1
            continue
        if received_ts > event_ts + maximum_delay:
            audit["excluded_late_books"] += 1
            continue
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        bid_depth = sum(max(0.0, _number(level.get("size"))) for level in bids[:5])
        ask_depth = sum(max(0.0, _number(level.get("size"))) for level in asks[:5])
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            audit["excluded_invalid_books"] += 1
            continue
        books.append({
            "event_ts": event_ts,
            "received_ts": received_ts,
            "service": payload.get("service"),
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "imbalance": (bid_depth - ask_depth) / total_depth,
        })
    books.sort(key=lambda row: (row["received_ts"], row["event_ts"]))
    return books, audit


def _latest_received(
    rows: list[dict[str, Any]], *, after_ts: int, cutoff_ts: int
) -> dict[str, Any] | None:
    known = [
        row
        for row in rows
        if after_ts <= row["received_ts"] <= cutoff_ts
        and row["event_ts"] <= cutoff_ts
    ]
    return known[-1] if known else None


def _signal_hype(signal: dict[str, Any]) -> dict[str, Any]:
    semantic = signal.get("semantic_signal") or {}
    return signal.get("hype") or semantic.get("hype") or {}


def _normalize_signals(
    rows: list[dict[str, Any]], *, start_ts: int, end_ts: int, minimum_score: float
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    audit = {
        "input_signals": len(rows),
        "excluded_signal_outside_window": 0,
        "excluded_future_signal_features": 0,
        "excluded_wrong_feature_version": 0,
        "excluded_below_hype_threshold": 0,
        "collapsed_signals": 0,
    }
    normalized = []
    seen = set()
    for row in sorted(rows, key=lambda item: _integer(item.get("ts"))):
        signal_ts = _integer(row.get("ts"))
        hype = _signal_hype(row)
        as_of_ts = _integer(hype.get("as_of_ts"))
        if signal_ts < start_ts or signal_ts > end_ts:
            audit["excluded_signal_outside_window"] += 1
            continue
        if as_of_ts <= 0 or as_of_ts > signal_ts:
            audit["excluded_future_signal_features"] += 1
            continue
        if hype.get("feature_version") != "news-hype-v1":
            audit["excluded_wrong_feature_version"] += 1
            continue
        score = _number(hype.get("long_hype_score"))
        if score < minimum_score:
            audit["excluded_below_hype_threshold"] += 1
            continue
        key = (signal_ts, str(row.get("symbol") or "").upper())
        if key in seen:
            audit["collapsed_signals"] += 1
            continue
        seen.add(key)
        normalized.append({
            **row,
            "ts": signal_ts,
            "hype_score": score,
            "feature_as_of_ts": as_of_ts,
        })
    return normalized, audit


def _adverse_fill(price: float, *, side: str, spread_bps: float, slippage_bps: float) -> float:
    bps = max(0.0, spread_bps) / 2 + max(0.0, slippage_bps)
    multiplier = 1 + bps / 10_000 if side == "buy" else 1 - bps / 10_000
    return price * multiplier


def _spread_bps(bar: dict[str, Any], assumed: float) -> float:
    bid = _number(bar.get("bid"))
    ask = _number(bar.get("ask"))
    midpoint = (bid + ask) / 2
    if bid > 0 and ask >= bid and midpoint > 0:
        return (ask - bid) / midpoint * 10_000
    return max(0.0, assumed)


def _volume_capacity(bar: dict[str, Any], fraction: float) -> int:
    return max(0, math.floor(max(0.0, _number(bar.get("volume"))) * max(0.0, fraction)))


def _simulate_trade(
    signal: dict[str, Any],
    bars: list[dict[str, Any]],
    entry_index: int,
    *,
    equity: float,
    config: ReplayConfig,
    quotes: list[dict[str, Any]],
    books: list[dict[str, Any]],
    eligible_after: int,
) -> dict[str, Any] | None:
    entry_bar = bars[entry_index]
    quote = _latest_received(
        quotes,
        after_ts=eligible_after,
        cutoff_ts=entry_bar["received_ts"],
    )
    book = _latest_received(
        books,
        after_ts=eligible_after,
        cutoff_ts=(quote or entry_bar)["received_ts"],
    )
    reference_price = quote["midpoint"] if quote else entry_bar["open"]
    signal_price = _number(
        signal.get("price") or (signal.get("market_signal") or {}).get("price")
    )
    if signal_price > 0:
        chase_pct = (reference_price / signal_price - 1) * 100
        if chase_pct > config.maximum_entry_chase_pct:
            return None
    spread_bps = quote["spread_bps"] if quote else _spread_bps(
        entry_bar, config.assumed_spread_bps
    )
    entry_price = _adverse_fill(
        reference_price,
        side="buy",
        spread_bps=spread_bps,
        slippage_bps=config.entry_slippage_bps,
    )
    requested_stop = _number(
        signal.get("stop_price")
        or (signal.get("trade_plan") or {}).get("stop_loss")
        or (signal.get("trade_plan") or {}).get("stop")
    )
    hard_stop = requested_stop if 0 < requested_stop < entry_price else entry_price * (
        1 - config.stop_loss_pct
    )
    risk_per_share = entry_price - hard_stop
    if risk_per_share <= 0:
        return None
    risk_quantity = math.floor(equity * config.risk_per_trade_pct / risk_per_share)
    position_quantity = math.floor(equity * config.max_position_pct / entry_price)
    liquid_quantity = _volume_capacity(entry_bar, config.entry_volume_participation_pct)
    requested_quantity = min(risk_quantity, position_quantity)
    explicit_quantity = _integer(signal.get("quantity"))
    if explicit_quantity > 0:
        requested_quantity = min(requested_quantity, explicit_quantity)
    filled_quantity = min(requested_quantity, liquid_quantity)
    if requested_quantity <= 0 or filled_quantity <= 0:
        return None

    entry_fee_per_share = entry_price * max(0.0, config.fee_bps) / 10_000
    remaining = filled_quantity
    partial_target_quantity = max(
        1, min(filled_quantity, math.floor(filled_quantity * config.partial_fraction))
    )
    partial_target_remaining = partial_target_quantity
    partial_taken = False
    peak = entry_price
    active_stop = hard_stop
    target = entry_price + config.partial_profit_r * risk_per_share
    realized_pnl = 0.0
    exit_fills = []
    pending_reason = None
    pending_reference = None
    halt_bars = 0
    gap_exit = False
    partial_exit = False
    final_index = entry_index

    for index in range(entry_index + 1, len(bars)):
        bar = bars[index]
        final_index = index
        if bar["halted"]:
            halt_bars += 1
            continue
        desired = 0
        reason = pending_reason
        reference = bar["open"] if pending_reason else None
        if pending_reason:
            desired = remaining if pending_reason != "partial_profit" else partial_target_remaining
        elif bar["open"] <= active_stop:
            desired = remaining
            reason = "stop_gap"
            reference = bar["open"]
            gap_exit = True
        elif bar["low"] <= active_stop:
            desired = remaining
            reason = "stop"
            reference = active_stop
        elif not partial_taken and bar["high"] >= target:
            desired = partial_target_remaining
            reason = "partial_profit"
            reference = target

        if desired > 0 and reference:
            capacity = _volume_capacity(bar, config.exit_volume_participation_pct)
            filled = min(desired, remaining, capacity)
            if filled > 0:
                exit_price = _adverse_fill(
                    reference,
                    side="sell",
                    spread_bps=_spread_bps(bar, config.assumed_spread_bps),
                    slippage_bps=config.exit_slippage_bps,
                )
                fee_per_share = exit_price * max(0.0, config.fee_bps) / 10_000
                pnl = filled * (
                    exit_price - entry_price - entry_fee_per_share - fee_per_share
                )
                realized_pnl += pnl
                remaining -= filled
                exit_fills.append({
                    "event_ts": bar["event_ts"],
                    "quantity": filled,
                    "price": round(exit_price, 6),
                    "reason": reason,
                })
                if filled < desired:
                    partial_exit = True
                if reason == "partial_profit":
                    partial_target_remaining -= filled
                    if partial_target_remaining <= 0:
                        partial_taken = True
                        pending_reason = None
                    else:
                        pending_reason = reason
                        pending_reference = reference
                elif remaining > 0:
                    pending_reason = reason
                    pending_reference = reference
                else:
                    pending_reason = None
            elif reason:
                pending_reason = reason
                pending_reference = reference
        if remaining <= 0:
            break

        peak = max(peak, bar["high"])
        peak_r = (peak - entry_price) / risk_per_share
        if peak_r >= config.break_even_r:
            active_stop = max(active_stop, entry_price)
        if peak_r >= config.runner_activation_r:
            active_stop = max(
                active_stop, peak - config.runner_trail_r * risk_per_share
            )

    last_bar = bars[final_index]
    unrealized_pnl = remaining * (
        last_bar["close"] - entry_price - entry_fee_per_share
    )
    total_pnl = realized_pnl + unrealized_pnl
    initial_risk_amount = filled_quantity * risk_per_share
    return {
        "signal_ts": signal["ts"],
        "feature_as_of_ts": signal["feature_as_of_ts"],
        "hype_score": round(signal["hype_score"], 6),
        "entry_event_ts": quote["event_ts"] if quote else entry_bar["event_ts"],
        "entry_received_ts": quote["received_ts"] if quote else entry_bar["received_ts"],
        "entry_latency_seconds": (
            quote["received_ts"] if quote else entry_bar["event_ts"]
        ) - signal["ts"],
        "entry_price_source": "levelone_quote" if quote else "minute_open_fallback",
        "entry_spread_bps": round(spread_bps, 6),
        "entry_quote_age_seconds": (
            max(0, quote["received_ts"] - quote["event_ts"]) if quote else None
        ),
        "entry_book_service": book.get("service") if book else None,
        "entry_bid_depth": round(book["bid_depth"], 3) if book else None,
        "entry_ask_depth": round(book["ask_depth"], 3) if book else None,
        "entry_book_imbalance": round(book["imbalance"], 6) if book else None,
        "requested_quantity": requested_quantity,
        "filled_quantity": filled_quantity,
        "entry_fill_rate": round(filled_quantity / requested_quantity, 6),
        "partial_entry": filled_quantity < requested_quantity,
        "entry_price": round(entry_price, 6),
        "hard_stop": round(hard_stop, 6),
        "active_stop": round(active_stop, 6),
        "target": round(target, 6),
        "exit_fills": exit_fills,
        "remaining_quantity": remaining,
        "status": "closed" if remaining <= 0 else "open_at_end",
        "realized_pnl": round(realized_pnl, 6),
        "unrealized_pnl": round(unrealized_pnl, 6),
        "total_pnl": round(total_pnl, 6),
        "r_multiple": round(total_pnl / initial_risk_amount, 6),
        "halt_bars": halt_bars,
        "gap_exit": gap_exit,
        "partial_exit": partial_exit,
        "pending_exit_reason": pending_reason,
        "pending_exit_reference": pending_reference,
        "last_event_ts": last_bar["event_ts"],
    }


def run_news_hype_replay(
    symbol: str,
    *,
    signals: list[dict[str, Any]],
    market_observations: list[dict[str, Any]],
    start_ts: int,
    end_ts: int,
    config: ReplayConfig | None = None,
) -> dict[str, Any]:
    """Evaluate ranking signals without invoking a broker, model, network, or clock."""
    config = _validated_config(config or ReplayConfig())
    start = int(start_ts)
    end = int(end_ts)
    if start <= 0 or end <= start:
        raise ValueError("end_ts must be greater than start_ts")
    if end - start > 366 * 86_400:
        raise ValueError("replay window cannot exceed 366 days")
    if len(signals) > 5_000 or len(market_observations) > 100_000:
        raise ValueError("replay input exceeds bounded limits")

    bar_rows = [
        row for row in market_observations
        if str(row.get("observation_type") or "minute_candle").lower() == "minute_candle"
    ]
    quote_rows = [
        row for row in market_observations
        if str(row.get("observation_type") or "").lower() == "levelone_quote"
    ]
    book_rows = [
        row for row in market_observations
        if str(row.get("observation_type") or "").lower() == "level2_book"
    ]
    bars, market_audit = _normalize_bars(
        bar_rows,
        end_ts=end,
        maximum_delay=max(1, int(config.maximum_data_delay_seconds)),
    )
    market_audit["input_market_observations"] = len(market_observations)
    quotes, quote_audit = _normalize_quotes(
        quote_rows,
        end_ts=end,
        maximum_delay=max(1, int(config.maximum_data_delay_seconds)),
    )
    books, book_audit = _normalize_books(
        book_rows,
        end_ts=end,
        maximum_delay=max(1, int(config.maximum_data_delay_seconds)),
    )
    candidates, signal_audit = _normalize_signals(
        signals,
        start_ts=start,
        end_ts=end,
        minimum_score=max(0.0, config.minimum_hype_score),
    )
    trades = []
    skipped_open_position = 0
    halted_entries = 0
    expired_entries = 0
    last_position_event_ts = start - 1
    equity = config.initial_equity

    for signal in candidates:
        if signal["ts"] <= last_position_event_ts:
            skipped_open_position += 1
            continue
        eligible_after = signal["ts"] + max(0, int(config.entry_latency_seconds))
        entry_index = next(
            (
                index
                for index, bar in enumerate(bars)
                if bar["event_ts"] > eligible_after
            ),
            None,
        )
        if entry_index is None:
            expired_entries += 1
            continue
        entry_bar = bars[entry_index]
        if entry_bar["event_ts"] - signal["ts"] > config.maximum_entry_wait_seconds:
            expired_entries += 1
            continue
        if entry_bar["halted"]:
            halted_entries += 1
            continue
        entry_quote = _latest_received(
            quotes, after_ts=eligible_after, cutoff_ts=entry_bar["received_ts"]
        )
        if entry_quote and entry_quote["halted"]:
            halted_entries += 1
            continue
        trade = _simulate_trade(
            signal,
            bars,
            entry_index,
            equity=equity,
            config=config,
            quotes=quotes,
            books=books,
            eligible_after=eligible_after,
        )
        if trade is None:
            continue
        trades.append(trade)
        equity += trade["total_pnl"]
        last_position_event_ts = trade["last_event_ts"]

    pnl_values = [trade["total_pnl"] for trade in trades]
    positive = sum(value for value in pnl_values if value > 0)
    negative = -sum(value for value in pnl_values if value < 0)
    closed_equity = config.initial_equity
    peak_equity = closed_equity
    maximum_drawdown = 0.0
    for value in pnl_values:
        closed_equity += value
        peak_equity = max(peak_equity, closed_equity)
        maximum_drawdown = max(maximum_drawdown, peak_equity - closed_equity)
    trade_count = len(trades)
    wins = sum(value > 0 for value in pnl_values)
    fill_rate = (
        sum(trade["filled_quantity"] for trade in trades)
        / sum(trade["requested_quantity"] for trade in trades)
        if trades
        else 0.0
    )
    profit_factor = positive / negative if negative > 0 else (None if positive <= 0 else 999.0)
    metrics = {
        "trade_count": trade_count,
        "closed_trade_count": sum(trade["status"] == "closed" for trade in trades),
        "open_at_end_count": sum(trade["status"] != "closed" for trade in trades),
        "wins": wins,
        "losses": sum(value < 0 for value in pnl_values),
        "win_rate": round(wins / trade_count, 6) if trade_count else 0.0,
        "net_pnl": round(sum(pnl_values), 6),
        "return_pct": round(sum(pnl_values) / config.initial_equity * 100, 6),
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
        "average_r": round(
            sum(trade["r_multiple"] for trade in trades) / trade_count, 6
        ) if trade_count else 0.0,
        "maximum_closed_equity_drawdown_pct": round(
            maximum_drawdown / config.initial_equity * 100, 6
        ),
        "entry_fill_rate": round(fill_rate, 6),
        "partial_entry_count": sum(trade["partial_entry"] for trade in trades),
        "partial_exit_count": sum(trade["partial_exit"] for trade in trades),
        "gap_exit_count": sum(trade["gap_exit"] for trade in trades),
        "halt_bar_count": sum(trade["halt_bars"] for trade in trades),
    }
    acceptance_checks = {
        "minimum_trade_count": trade_count >= config.minimum_acceptance_trades,
        "no_open_positions_at_end": metrics["open_at_end_count"] == 0,
        "minimum_profit_factor": (
            profit_factor is not None and profit_factor >= config.minimum_profit_factor
        ),
        "positive_average_r": metrics["average_r"] > 0,
        "minimum_fill_rate": fill_rate >= config.minimum_fill_rate,
        "maximum_drawdown": (
            metrics["maximum_closed_equity_drawdown_pct"]
            <= config.maximum_drawdown_pct
        ),
    }
    return {
        "replay_version": REPLAY_VERSION,
        "acceptance_version": ACCEPTANCE_VERSION,
        "mode": "historical_replay",
        "ranking_only": True,
        "broker_calls": 0,
        "symbol": str(symbol).upper(),
        "start_ts": start,
        "end_ts": end,
        "config": asdict(config),
        "audit": {
            **market_audit,
            **quote_audit,
            **book_audit,
            **signal_audit,
            "eligible_signals": len(candidates),
            "skipped_open_position": skipped_open_position,
            "halted_entries": halted_entries,
            "expired_entries": expired_entries,
        },
        "metrics": metrics,
        "acceptance": {
            "scope": "candidate_model_research_only",
            "eligible": trade_count >= config.minimum_acceptance_trades,
            "passed": all(acceptance_checks.values()),
            "checks": acceptance_checks,
        },
        "live_activation_eligible": False,
        "trades": trades,
    }
