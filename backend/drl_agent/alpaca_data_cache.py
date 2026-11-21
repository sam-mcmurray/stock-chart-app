"""Bounded, download-once SIP/raw minute samples; market-data endpoint only."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile

from dotenv import dotenv_values
import requests

from drl_agent.dataset import encode_dataset

URL = "https://data.alpaca.markets/v2/stocks/bars"


class NoBarsError(ValueError):
    """The provider successfully returned no bars for this window."""


class MarketDataHTTPError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Alpaca market-data request returned HTTP {status_code}")


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Explicit timestamp timezone required")
    return result.astimezone(timezone.utc)


def cache_bars(symbol, start, end, output, *, key=None, secret=None, get=requests.get):
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol):
        raise ValueError("Invalid symbol")
    start, end = _time(start), _time(end)
    if (not timedelta(0) < end - start <= timedelta(days=31)
            or start.second or start.microsecond or end.second or end.microsecond):
        raise ValueError("Require minute-aligned window of at most 31 days")
    params = {"symbols": symbol, "timeframe": "1Min", "feed": "sip", "adjustment": "raw",
              "asof": "-", "start": start.isoformat(), "end": (end - timedelta(seconds=1)).isoformat(),
              "sort": "asc", "limit": 10000}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}_{start:%Y%m%dT%H%MZ}_{end:%Y%m%dT%H%MZ}_sip_raw.json"
    target, lock = output / name, output / ("." + name + ".lock")
    lock_handle = lock.open("a")
    temporary = None
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if target.exists():
            saved = json.loads(target.read_text())
            digest = saved.pop("archive_sha256")
            if hashlib.sha256(encode_dataset(saved).encode()).hexdigest() != digest or saved["request"] != params:
                raise ValueError("Local cache verification failed; not replaced")
            return {"file": str(target), "reused": True, "archive_sha256": digest, **saved["summary"]}
        if not key or not secret:
            raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in backend/.env")
        pages, bars, tokens, page_token = [], [], set(), None
        previous = None
        for _ in range(100):
            query = {**params, **({"page_token": page_token} if page_token else {})}
            try:
                with get(URL, params=query, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                         timeout=(15, 60), allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise MarketDataHTTPError(response.status_code)
                    if len(response.content) > 16 * 1024 * 1024:
                        raise ValueError("Response exceeds sample size limit")
                    payload = response.json()
            except requests.RequestException:
                raise RuntimeError("Alpaca market-data connection failed; credentials not logged") from None
            page = (payload.get("bars") or {}).get(symbol, [])
            if not isinstance(page, list) or set(payload.get("bars") or {}) - {symbol}:
                raise ValueError("Unexpected bars response")
            for bar in page:
                stamp = _time(bar["t"])
                values = [bar[k] for k in ("o", "h", "l", "c", "v")]
                if (not start <= stamp < end or stamp.second or stamp.microsecond
                        or (previous is not None and stamp <= previous)
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                        or min(values[:4]) <= 0 or values[4] < 0
                        or bar["h"] < max(values[:4]) or bar["l"] > min(values[:4])):
                    raise ValueError("Invalid, duplicate or nonchronological OHLCV")
                previous = stamp
            bars.extend(page)
            pages.append(payload)
            if len(bars) > 100000:
                raise ValueError("Sample exceeds row budget")
            page_token = payload.get("next_page_token")
            if not page_token:
                break
            if not isinstance(page_token, str) or page_token in tokens:
                raise ValueError("Invalid/repeated pagination token")
            tokens.add(page_token)
        else:
            raise ValueError("Pagination limit exceeded")
        if not bars:
            raise NoBarsError("No bars returned; no cache published")
        summary = {"symbol": symbol, "rows": len(bars), "pages": len(pages),
                   "first_timestamp": bars[0]["t"], "last_timestamp": bars[-1]["t"],
                   "volume_sum": sum(b["v"] for b in bars),
                   "first_close": bars[0]["c"], "last_close": bars[-1]["c"]}
        result = {"version": "alpaca-sip-raw-cache-v1", "url": URL, "request": params,
                  "archived_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                  "responses": pages, "training_ready": False,
                  "limitations": ["Market data only; no account or order endpoints called",
                      "Raw nominal prices; symbol mapping disabled to preserve requested historical ticker",
                      "No missing bars filled; session completeness not certified by download"]}
        digest = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
        result["archive_sha256"] = digest
        fd, path = tempfile.mkstemp(prefix=".alpaca_", suffix=".part", dir=output)
        temporary = Path(path)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encode_dataset(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        return {"file": str(target), "reused": False, "archive_sha256": digest, **summary}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        # Keep the inode stable for competing readers; closing releases crash-safe ownership.
        lock_handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    result = cache_bars(args.symbol, args.start, args.end, args.output,
                        key=os.getenv("APCA_API_KEY_ID") or env.get("APCA_API_KEY_ID"),
                        secret=os.getenv("APCA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY"))
    print(encode_dataset(result))


if __name__ == "__main__":
    main()
