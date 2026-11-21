"""Seeded bar-resolution depth scenarios, not reconstructed exchange observations."""
from dataclasses import asdict, dataclass
import argparse
import hashlib
import json
import math
from pathlib import Path
import random

from drl_agent.dataset import encode_dataset

VERSION = "synthetic-depth-v1"


@dataclass(frozen=True)
class DepthConfig:
    seed: int = 0
    levels: int = 5
    tick_size: float = 0.01
    spread_bps: float = 20.0
    displayed_volume_fraction: float = 0.02
    cancellation_fraction: float = 0.2
    replenishment_fraction: float = 0.2
    order_volume_fraction: float = 0.01

    def __post_init__(self):
        if type(self.seed) is not int or not 0 <= self.seed < 2**63:
            raise ValueError("seed must be an integer in 0..2**63-1")
        if type(self.levels) is not int or not 1 <= self.levels <= 20:
            raise ValueError("levels must be in 1..20")
        for name in ("tick_size", "spread_bps", "displayed_volume_fraction",
                     "cancellation_fraction", "replenishment_fraction", "order_volume_fraction"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid {name}")
        if not 0 < self.tick_size <= 1 or not 0 < self.spread_bps <= 1000:
            raise ValueError("Invalid tick size or spread")
        if not 0 < self.displayed_volume_fraction <= 1 or not 0 < self.order_volume_fraction <= 1:
            raise ValueError("Invalid volume fraction")
        if not 0 <= self.cancellation_fraction <= 1 or not 0 <= self.replenishment_fraction <= 1:
            raise ValueError("Invalid cancellation or replenishment fraction")


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid {name}")
    return value


def generate_depth(symbol, candles, *, config=None):
    """Consume chronological completed bars; never consult later rows.

    Each input requires id, start_ts, end_ts, OHLC and volume. end_ts is the
    exclusive bar end, not import receipt time. Imported prices remain
    retrospective. This generator neither simulates fills nor emits live quotes.
    """
    config = config or DepthConfig()
    symbol = symbol.strip().upper()
    if not symbol or not candles or len(candles) > 100_000:
        raise ValueError("Provide a symbol and 1..100000 completed candles")
    seed = hashlib.sha256(f"{VERSION}|{symbol}|{config.seed}".encode()).digest()
    rng = random.Random(int.from_bytes(seed, "big"))
    snapshots, seen = [], set()
    previous_end, previous_close, trailing_volume, pressure = 0, None, None, 0.0
    for candle in candles:
        identity = candle.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError("Candle IDs must be nonempty and unique")
        start, end = candle.get("start_ts"), candle.get("end_ts")
        if type(start) is not int or type(end) is not int or not 0 < start < end or start < previous_end:
            raise ValueError("Candles must be chronological, nonoverlapping intervals")
        prices = [_number(candle.get(k), k) for k in ("open", "high", "low", "close")]
        opening, high, low, close = prices
        volume = _number(candle.get("volume"), "volume")
        if min(prices) <= 0 or high < max(opening, close, low) or low > min(opening, close) or not 0 <= volume <= 1e12:
            raise ValueError("Invalid OHLC or volume")
        if low < config.tick_size * (config.levels + 2):
            raise ValueError("Price too low for configured tick size and depth")
        seen.add(identity)
        # Per-minute normalization avoids interpreting a longer bar as deeper.
        volume_per_minute = volume * 60 / (end - start)
        trailing_volume = volume_per_minute if trailing_volume is None else 0.8 * trailing_volume + 0.2 * volume_per_minute
        change = 0 if previous_close is None else math.log(close / previous_close)
        pressure = max(-1, min(1, 0.5 * pressure + 0.25 * math.tanh(change * 100) + rng.uniform(-0.5, 0.5)))
        spread_ticks = max(1, math.ceil(close * config.spread_bps / 10000 / config.tick_size * rng.uniform(0.75, 1.5)))
        center_ticks = round(close / config.tick_size)
        bid_tick = center_ticks - max(1, spread_ticks // 2)
        ask_tick = bid_tick + spread_ticks
        if bid_tick - config.levels + 1 <= 0:
            raise ValueError("Spread/depth would produce nonpositive prices")
        sides, flows = {}, {}
        for side, direction in (("bids", 1), ("asks", -1)):
            # Negative pressure consumes more bids; positive pressure consumes asks.
            aggressive = int(trailing_volume * config.order_volume_fraction *
                             (1 - direction * pressure) * rng.lognormvariate(0, 0.6))
            remaining = aggressive
            levels, cancelled_total, added_total, consumed_total = [], 0, 0, 0
            for level in range(config.levels):
                base = int(trailing_volume * config.displayed_volume_fraction / config.levels *
                           (1 + 0.5 * direction * pressure) * rng.lognormvariate(0, 0.6))
                cancelled = int(base * config.cancellation_fraction * rng.random())
                added = int(base * config.replenishment_fraction * rng.random())
                available = base - cancelled + added
                consumed = min(available, remaining)
                remaining -= consumed
                price_tick = bid_tick - level if side == "bids" else ask_tick + level
                levels.append({"price": round(price_tick * config.tick_size, 10),
                               "size": available - consumed})
                cancelled_total += cancelled
                added_total += added
                consumed_total += consumed
            sides[side] = levels
            flows[side] = {"aggressive_order_shares": aggressive, "consumed_shares": consumed_total,
                           "unmatched_shares": remaining, "cancelled_shares": cancelled_total,
                           "replenished_shares": added_total}
        snapshots.append({"symbol": symbol, "source": VERSION, "synthetic": True,
                          "observation_type": "synthetic_level2_book", "available_ts": end,
                          "source_candle_id": identity, "source_bar_start_ts": start,
                          "source_bar_end_ts": end, "bids": sides["bids"], "asks": sides["asks"],
                          "scenario_flows": flows})
        previous_end, previous_close = end, close
    report = {"version": VERSION, "symbol": symbol, "config": asdict(config),
              "synthetic": True, "retrospective": True, "live_activation_eligible": False,
              "training_ready": False, "broker_calls": 0,
              "input_sha256": hashlib.sha256(encode_dataset(candles).encode()).hexdigest(),
              "snapshots": snapshots}
    report["scenario_sha256"] = hashlib.sha256(encode_dataset(report).encode()).hexdigest()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="JSON object with symbol, candles, and optional config")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    payload = json.loads(args.input.read_text())
    report = generate_depth(payload["symbol"], payload["candles"],
                            config=DepthConfig(**payload.get("config", {})))
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(report) + "\n")
    print(encode_dataset({k: v for k, v in report.items() if k != "snapshots"}))


if __name__ == "__main__":
    main()
