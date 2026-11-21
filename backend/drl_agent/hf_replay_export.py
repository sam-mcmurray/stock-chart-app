"""Offline raw-Parquet to completed historical candles and synthetic depth."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from drl_agent.dataset import encode_dataset
from drl_agent.synthetic_depth import DepthConfig, generate_depth


def window(start, end):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Window timestamps require explicit timezone offsets")
    if not start < end or end - start > pd.Timedelta(days=31):
        raise ValueError("Provide an increasing window of at most 31 days")
    if start.value % 1_000_000_000 or end.value % 1_000_000_000:
        raise ValueError("Window timestamps must use whole seconds")
    return start.tz_convert("UTC"), end.tz_convert("UTC")


def build_export(frame, provenance, *, start, end, depth_config=None):
    start, end = window(start, end)
    if provenance.get("version") != "raw" or provenance.get("provider") != "HF Data Library":
        raise ValueError("Only the raw HF archive is accepted")
    if not provenance.get("symbol") or not provenance.get("sha256"):
        raise ValueError("Missing archive identity")
    if len(frame) > 100000:
        raise ValueError("Too many rows; shorten the window")
    frame = frame.copy()
    dates = pd.to_datetime(frame["datetime"], errors="raise")
    if dates.isna().any() or dates.dt.tz is not None:
        raise ValueError("Expected nonmissing timezone-naive Eastern archive timestamps")
    # Raise for ambiguous/nonexistent DST times rather than guessing an instant.
    dates = dates.dt.tz_localize("America/New_York", ambiguous="raise", nonexistent="raise").dt.tz_convert("UTC")
    frame["utc"] = dates
    frame = frame[(dates >= start) & (dates + pd.Timedelta(minutes=1) <= end)].sort_values("utc")
    if frame.utc.duplicated().any():
        raise ValueError("Duplicate bar timestamps require explicit resolution")
    candles, rejected = [], Counter()
    for row in frame.itertuples(index=False):
        if row.utc.value % 60_000_000_000:
            rejected["unaligned_minute"] += 1
            continue
        try:
            values = [float(getattr(row, k)) for k in ("Open", "High", "Low", "Close", "Volume")]
        except (TypeError, ValueError):
            rejected["invalid_ohlcv"] += 1
            continue
        opening, high, low, close, volume = values
        if (not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or volume < 0
                or high < max(opening, low, close) or low > min(opening, close)):
            rejected["invalid_ohlcv"] += 1
            continue
        if row.source not in {"pitrading", "iex"}:
            rejected["unknown_source"] += 1
            continue
        ts = int(row.utc.timestamp())
        identity = hashlib.sha256(encode_dataset([provenance["sha256"], ts]).encode()).hexdigest()
        candles.append({"id": identity, "start_ts": ts, "end_ts": ts + 60,
                        "available_ts": ts + 60, "open": opening, "high": high, "low": low,
                        "close": close, "volume": volume, "source": row.source,
                        "retrospective": True})
    if not candles:
        raise ValueError("No valid completed candles in requested window")
    sources = dict(Counter(c["source"] for c in candles))
    if len(sources) != 1:
        raise ValueError("Split export at provider source boundary; do not blend volume regimes")
    result = {"version": "hf-historical-replay-inputs-v1", "symbol": provenance["symbol"],
              "source_file_sha256": provenance["sha256"],
              "source_archived_at_utc": provenance.get("archived_at_utc"),
              "window": {"start": start.isoformat(), "end": end.isoformat()},
              "availability_basis": "documented_bar_start_plus_60_seconds",
              "retrospective": True, "training_ready": False, "live_activation_eligible": False,
              "broker_calls": 0, "network_calls": 0, "source_rows": sources,
              "rejected_rows": dict(rejected), "candles": candles,
              "limitations": provenance.get("limitations", []),
              "execution_supported": False}
    if depth_config is not None:
        result["synthetic_depth"] = generate_depth(provenance["symbol"], candles, config=depth_config)
    result["export_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def export_archive(path, *, start, end, depth_config=None):
    path = Path(path)
    start, end = window(start, end)
    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    if provenance.get("file") != path.name:
        raise ValueError("Archive filename does not match provenance")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != provenance.get("sha256"):
        raise ValueError("Archive checksum mismatch")
    # Filter row groups locally, never load every decade for a short episode.
    first = start.tz_convert("America/New_York").tz_localize(None).to_pydatetime()
    last = end.tz_convert("America/New_York").tz_localize(None).to_pydatetime()
    frame = pd.read_parquet(path, columns=["datetime", "Open", "High", "Low", "Close", "Volume", "source"],
                            filters=[("datetime", ">=", first), ("datetime", "<", last)])
    return build_export(frame, provenance, start=start, end=end, depth_config=depth_config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic-depth", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = export_archive(args.archive, start=args.start, end=args.end,
                            depth_config=DepthConfig(seed=args.seed) if args.synthetic_depth else None)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({**{k: v for k, v in result.items() if k not in {"candles", "synthetic_depth"}},
                          "candle_count": len(result["candles"]),
                          "synthetic_snapshot_count": len(result.get("synthetic_depth", {}).get("snapshots", []))}))


if __name__ == "__main__":
    main()
