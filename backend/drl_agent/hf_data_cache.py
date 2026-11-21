"""Download-once raw HF Data Library Parquet archives; never replace saved data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import quote

from dotenv import dotenv_values
import pandas as pd
import pyarrow.parquet as pq
import requests

from drl_agent.dataset import encode_dataset

MAX_BYTES = 256 * 1024 * 1024


def inspect_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    parquet = pq.ParquetFile(path)
    columns = ["datetime", "Open", "High", "Low", "Close", "Volume", "source"]
    if not set(columns).issubset(parquet.schema_arrow.names):
        raise ValueError("Unexpected Parquet schema")
    data = parquet.read(columns=columns).to_pandas()
    if data.empty or not pd.api.types.is_datetime64_any_dtype(data.datetime):
        raise ValueError("Empty or invalid time series")
    import numpy as np
    values = data[["Open", "High", "Low", "Close", "Volume"]]
    finite = pd.Series(np.isfinite(values.to_numpy(dtype=float)).all(axis=1), index=data.index)
    prices = data[["Open", "High", "Low", "Close"]]
    invalid = (~finite | (prices <= 0).any(axis=1) | (data.Volume < 0)
               | (data.High < prices.max(axis=1)) | (data.Low > prices.min(axis=1)))
    return {"sha256": digest.hexdigest(), "size_bytes": path.stat().st_size,
            "rows": len(data), "columns": list(parquet.schema_arrow.names),
            "first_datetime": str(data.datetime.min()), "last_datetime": str(data.datetime.max()),
            "timestamp_timezone": str(data.datetime.dt.tz),
            "documented_timezone": "America/New_York",
            "missing_datetimes": int(data.datetime.isna().sum()),
            "duplicate_datetimes": int(data.datetime.duplicated().sum()),
            "chronological": bool(data.datetime.is_monotonic_increasing),
            "invalid_ohlcv_rows": int(invalid.sum()),
            "zero_volume_rows": int((data.Volume == 0).sum()),
            "missing_source_rows": int(data.source.isna().sum()),
            "source_rows": {str(k): int(v) for k, v in data.source.value_counts().items()}}


def cache_raw(symbol, output, *, api_key=None, get=requests.get):
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.^-]{0,19}", symbol):
        raise ValueError("Invalid ticker")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{symbol}_raw.parquet"
    provenance = output / f"{symbol}_raw.provenance.json"
    lock = output / f".{symbol}_raw.lock"
    # An exclusive lock also prevents duplicate network calls from concurrent runs.
    with lock.open("x"):
        pass
    temporary = None
    try:
        reused = target.exists()
        url = f"https://api.hfdatalibrary.com/v1/bars/{quote(symbol, safe='')}?version=raw"
        if not reused:
            if not api_key:
                raise ValueError("Set HF_DATA in backend/.env before downloading")
            fd, name = tempfile.mkstemp(prefix=f".{symbol}_", suffix=".part", dir=output)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                with get(url, headers={"X-API-Key": api_key}, stream=True,
                         timeout=(20, 60), allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"HF Data download returned HTTP {response.status_code}")
                    count = 0
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        count += len(chunk)
                        if count > MAX_BYTES:
                            raise ValueError("Download exceeds file-size limit")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            details = inspect_file(temporary)
            # Same-filesystem link publishes the verified file without overwriting.
            os.link(temporary, target)
        else:
            details = inspect_file(target)
        if provenance.exists():
            prior = json.loads(provenance.read_text())
            if prior["sha256"] != details["sha256"]:
                raise ValueError("Saved file checksum mismatch; retained without redownload")
        else:
            prior = {"provider": "HF Data Library", "symbol": symbol, "version": "raw",
                     "frequency_minutes": 1, "url": url, "file": target.name,
                     "archived_at_utc": datetime.now(timezone.utc).isoformat(),
                     "training_ready": False, **details,
                     "citation": "Elkassabgi, A. (2026). HF Data Library: Free 1-Minute Intraday U.S. Equity Data. https://doi.org/10.5281/zenodo.19501605",
                     "documentation": "https://hfdatalibrary.com/pages/docs",
                     "limitations": ["Raw bars need causal cleaning before replay",
                                      "IEX volume is single-venue, not consolidated",
                                      "Corporate-action adjustments require historical price-filter care",
                                      "Fixed universe has survivorship bias"]}
            with provenance.open("x", encoding="utf-8") as stream:
                stream.write(encode_dataset(prior) + "\n")
        return {"file": str(target), "reused_local_file": reused, **details}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="Tickers to archive as raw Parquet")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    key = os.getenv("HF_DATA") or config.get("HF_DATA")
    import time
    for index, symbol in enumerate(dict.fromkeys(args.symbols)):
        if index:
            time.sleep(1)
        print(encode_dataset(cache_raw(symbol, args.output, api_key=key)), flush=True)


if __name__ == "__main__":
    main()
