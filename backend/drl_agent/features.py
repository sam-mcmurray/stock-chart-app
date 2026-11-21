"""Versioned, bounded market features for offline DRL observation datasets."""
from __future__ import annotations

import math

from drl_agent.dataset import VERSION as DATASET_VERSION, _normalize

VERSION = "drl-market-features-v1"
FAMILIES = ("scanner", "levelone_quote", "level2_book", "minute_candle")
FRESH_MS = dict(zip(FAMILIES, (900_000, 3000, 2000, 120_000)))
SCANNER_FIELDS = (
    ("rvol", ("market_signal", "rvol"), 10),
    ("rsi", ("market_signal", "rsi_14"), 100),
    ("momentum_5d", ("market_signal", "momentum_5d_pct"), 100),
    ("volatility", ("market_signal", "volatility_20"), 1),
    ("gap", ("market_signal", "intraday", "gap_pct"), 100),
    ("volume_pace", ("market_signal", "intraday", "volume_pace"), 10),
    ("attention", ("semantic_signal", "hype", "attention_score"), 1),
    ("long_hype", ("semantic_signal", "hype", "long_hype_score"), 1),
    ("news_recency", ("semantic_signal", "hype", "recency_score"), 1),
    ("news_novelty", ("semantic_signal", "hype", "novelty_score"), 1),
    ("news_breadth", ("semantic_signal", "hype", "source_breadth_score"), 1),
    ("mention_velocity", ("semantic_signal", "hype", "mention_velocity_score"), 1),
    ("news_direction", ("semantic_signal", "hype", "directional_score"), 1),
)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _get(row, path):
    for key in path:
        if not isinstance(row, dict):
            return None
        row = row.get(key)
    return row


def _nbbo(row):
    bid, ask = _number(row.get("bid")), _number(row.get("ask"))
    if bid is None or ask is None or not 0 < bid <= ask:
        return None
    return (bid + ask) / 2, (ask - bid) / ((bid + ask) / 2)


def encode_state(state: dict) -> dict:
    cutoff = int(state["decision_ts"]) * 1000
    symbol = str(state["symbol"]).upper()
    histories, rejected = {}, 0
    for kind in FAMILIES:
        accepted = []
        for item in state.get("inputs", {}).get(kind, []):
            try:
                raw = item["record"]
                row = _normalize(raw, scanner=kind == "scanner")
                if (str(raw.get("symbol", "")).upper() != symbol
                        or raw.get("source") != ("scanner_shadow" if kind == "scanner" else "schwab_stream")
                        or row["kind"] != kind or row["available_ms"] > cutoff):
                    raise ValueError("Unavailable input")
                accepted.append(row)
            except (KeyError, ValueError, TypeError, OverflowError):
                rejected += 1
        histories[kind] = sorted(accepted, key=lambda r: (r["event_ms"], r["received_ms"], r["id"]))
    values, masks, names = [], [], []

    def add(name, value, scale=1):
        value = _number(value)
        names.append(name)
        masks.append(int(value is not None))
        values.append(max(-1.0, min(1.0, value / scale)) if value is not None else 0.0)

    def latest(kind):
        return histories[kind][-1] if histories[kind] else None

    for kind in FAMILIES:
        row = latest(kind)
        add(f"{kind}.present", int(row is not None))
        event_age = cutoff - row["event_ms"] if row else None
        receive_age = cutoff - row["received_ms"] if row else None
        add(f"{kind}.event_age", event_age, 900_000)
        add(f"{kind}.receive_age", receive_age, 900_000)
        add(f"{kind}.fresh", int(row is not None and max(event_age, receive_age) <= FRESH_MS[kind]))

    scanner = (latest("scanner") or {}).get("record", {}).get("payload", {})
    for name, path, scale in SCANNER_FIELDS:
        add("scanner." + name, _get(scanner, path), scale)
    price = _number(_get(scanner, ("market_signal", "price")))
    vwap = _number(_get(scanner, ("market_signal", "intraday", "vwap")))
    add("scanner.vwap_distance", price / vwap - 1 if price and vwap and vwap > 0 else None, 0.1)

    quote = (latest("levelone_quote") or {}).get("record", {}).get("payload", {})
    nbbo = _nbbo(quote)
    add("quote.valid_nbbo", int(nbbo is not None))
    add("quote.spread", nbbo[1] if nbbo else None, 0.01)
    bid_size, ask_size = (_number(quote.get(k)) for k in ("bid_size_shares", "ask_size_shares"))
    sizes_valid = bid_size is not None and ask_size is not None and min(bid_size, ask_size) >= 0
    total = bid_size + ask_size if sizes_valid else 0
    add("quote.size_imbalance", (bid_size - ask_size) / total if total > 0 else None)
    add("quote.day_change", quote.get("net_percent_change"), 100)
    add("quote.delayed", int(quote["delayed"]) if isinstance(quote.get("delayed"), bool) else None)
    add("quote.normal_status", int(str(quote["security_status"]).lower() == "normal") if quote.get("security_status") else None)
    for field in ("quote_time_ms", "bid_time_ms", "ask_time_ms", "trade_time_ms"):
        timestamp = _number(quote.get(field))
        add("quote." + field.replace("_time_ms", "_age"), cutoff - timestamp if timestamp and timestamp <= cutoff else None, 900_000)
    prior_quotes = histories["levelone_quote"][:-1]
    previous = prior_quotes[-1] if prior_quotes else None
    old_nbbo = _nbbo(previous["record"].get("payload", {})) if previous else None
    add("quote.mid_return", nbbo[0] / old_nbbo[0] - 1 if nbbo and old_nbbo else None, 0.01)
    add("quote.return_interval", latest("levelone_quote")["event_ms"] - previous["event_ms"] if previous else None, 60_000)

    book = (latest("level2_book") or {}).get("record", {}).get("payload", {})
    sides = {}
    for side in ("bids", "asks"):
        levels = book.get(side) or []
        valid = []
        for level in levels[:5]:
            p, size = _number(level.get("price")), _number(level.get("size"))
            if p is None or size is None or p <= 0 or size < 0:
                valid = []
                break
            valid.append((p, size))
        prices = [p for p, _ in valid]
        sides[side] = valid if prices == sorted(prices, reverse=side == "bids") else []
    bids, asks = sides["bids"], sides["asks"]
    book_valid = bool(bids and asks and bids[0][0] <= asks[0][0])
    midpoint = (bids[0][0] + asks[0][0]) / 2 if book_valid else None
    add("book.valid", int(book_valid))
    add("book.nasdaq", int(book.get("service") == "NASDAQ_BOOK") if book else None)
    add("book.nyse", int(book.get("service") == "NYSE_BOOK") if book else None)
    depth = {s: sum(size for _, size in sides[s]) for s in sides}
    total_depth = sum(depth.values())
    add("book.imbalance", (depth["bids"] - depth["asks"]) / total_depth if book_valid and total_depth else None)
    for side in ("bids", "asks"):
        add(f"book.{side}.levels", len(sides[side]) if book_valid else None, 5)
        add(f"book.{side}.depth_log", math.log1p(depth[side]) if book_valid else None, 20)
        for index in range(5):
            level = sides[side][index] if book_valid and index < len(sides[side]) else None
            add(f"book.{side}.{index}.distance", level[0] / midpoint - 1 if level else None, 0.1)
            add(f"book.{side}.{index}.size_log", math.log1p(level[1]) if level else None, 20)

    candles = histories["minute_candle"]
    candle = candles[-1]["record"] if candles else {}
    o, h, low, c, volume = (_number(candle.get(k)) for k in ("open", "high", "low", "close", "volume"))
    valid = all(v is not None for v in (o, h, low, c)) and 0 < low <= min(o, c) <= max(o, c) <= h
    add("candle.valid", int(valid))
    add("candle.body", c / o - 1 if valid else None, 0.1)
    add("candle.range", (h - low) / o if valid else None, 0.1)
    add("candle.volume_log", math.log1p(volume) if valid and volume is not None and volume >= 0 else None, 20)
    return {"version": VERSION, "symbol": symbol, "decision_ts": state["decision_ts"],
            "names": names, "values": values, "valid": masks, "rejected_inputs": rejected,
            "source_ids": {kind: [r["id"] for r in rows] for kind, rows in histories.items()}}


def encode_observations(dataset: dict) -> dict:
    if dataset.get("manifest", {}).get("version") != DATASET_VERSION:
        raise ValueError("Unsupported observation dataset version")
    rows = [encode_state(state) for state in dataset["states"]]
    names = rows[0]["names"] if rows else []
    return {"manifest": {"version": VERSION, "source_dataset_sha256": dataset["manifest"].get("dataset_sha256"),
                         "feature_names": names, "feature_count": len(names), "state_count": len(rows),
                         "training_ready": False, "broker_calls": 0},
            "states": [{k: v for k, v in row.items() if k != "names"} for row in rows]}
