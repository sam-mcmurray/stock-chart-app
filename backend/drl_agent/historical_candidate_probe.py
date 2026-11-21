"""Bounded historical-price acquisition checks for a frozen news universe."""
import argparse
from datetime import timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import time

from dotenv import dotenv_values

from drl_agent.alpaca_data_cache import cache_bars, _time
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def probe(manifest, start, output, *, minimum_price, maximum_price, offset=0, limit=20,
          key=None, secret=None, cache=cache_bars, sleep=time.sleep):
    _verify(manifest, "retrospective-news-universe-v1", "manifest_sha256")
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in (minimum_price, maximum_price))
            or not 0 < minimum_price <= maximum_price):
        raise ValueError("Invalid probe limits")
    start = _time(start)
    if start.second or start.microsecond:
        raise ValueError("Probe start must be minute-aligned")
    end = start + timedelta(minutes=1)
    symbols = sorted({r["symbol"] for r in manifest["price_requests"]})
    rows = []
    for symbol in symbols[offset:offset + limit]:
        try:
            result = cache(symbol, start.isoformat(), end.isoformat(), output, key=key, secret=secret)
        except ValueError as exc:
            if str(exc) != "No bars returned; no cache published":
                raise
            rows.append({"symbol": symbol, "status": "no_bar_at_probe_time"})
            sleep(.25)
            continue
        value = result["first_close"]
        rows.append({"symbol": symbol, "status": "price_in_range" if minimum_price <= value <= maximum_price
                     else "price_out_of_range", "close": value, "price_basis": "nominal",
                     "available_ts": int(end.timestamp()), "archive_sha256": result["archive_sha256"],
                     "archive_file": Path(result["file"]).name, "cache_reused": result["reused"]})
        if not result["reused"]:
            sleep(.25)
    result = {"version": "historical-candidate-probe-v1", "source_manifest_sha256": manifest["manifest_sha256"],
              "universe_count": len(symbols), "offset": offset, "next_offset": min(offset + len(rows), len(symbols)),
              "start": start.isoformat(), "end": end.isoformat(), "minimum_price": minimum_price,
              "maximum_price": maximum_price, "rows": rows, "training_ready": False,
              "limitations": ["One-minute price probe is acquisition triage, not permanent universe selection",
                  "No claims of news availability, float/RVOL qualification or profitability at probe time",
                  "Missing bars do not prove delisting or lack of later trading; inspect other windows",
                  "Frozen news universe retains its original retrospective enrichment limitations"]}
    result["probe_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-price", type=float, required=True)
    parser.add_argument("--maximum-price", type=float, required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    result = probe(json.loads(args.manifest.read_text()), args.start, args.cache,
        minimum_price=args.minimum_price, maximum_price=args.maximum_price, offset=args.offset, limit=args.limit,
        key=os.getenv("APCA_API_KEY_ID") or env.get("APCA_API_KEY_ID"),
        secret=os.getenv("APCA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY"))
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({"checked": len(result["rows"]), "universe": result["universe_count"],
                         "next_offset": result["next_offset"],
                         "in_range": [r["symbol"] for r in result["rows"] if r["status"] == "price_in_range"]}))


if __name__ == "__main__":
    main()
