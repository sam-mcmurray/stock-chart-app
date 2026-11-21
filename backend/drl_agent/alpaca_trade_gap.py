"""Cache bounded SIP trade evidence for a missing minute; never invent a bar."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from dotenv import dotenv_values
import requests

from drl_agent.alpaca_data_cache import _time, MarketDataHTTPError
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def stamp_ns(value):
    match = re.fullmatch(r"(.+?)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise ValueError("Invalid trade timestamp")
    return int(_time(match[1] + match[3]).timestamp()) * 10**9 + int((match[2] or "").ljust(9, "0"))


def cache_minute(symbol, start, output, *, key=None, secret=None, get=requests.get):
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol):
        raise ValueError("Invalid symbol")
    start = _time(start)
    if start.second or start.microsecond:
        raise ValueError("Require minute-aligned start")
    end = start + timedelta(minutes=1)
    low, high = int(start.timestamp()) * 10**9, int(end.timestamp()) * 10**9
    params = {"start": start.isoformat(), "end": (end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S") + ".999999999Z",
              "feed": "sip", "asof": "-", "sort": "asc", "limit": 10000}
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/trades"
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{symbol}_{start:%Y%m%dT%H%MZ}_sip_trades.json"
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            saved = json.loads(path.read_text())
            _verify(saved, "alpaca-trade-gap-v1", "archive_sha256")
            if saved["request"] != params or saved["url"] != url:
                raise ValueError("Cached trade request mismatch")
            return {"file": str(path), "reused": True, **saved["summary"]}
        if not key or not secret:
            raise ValueError("Missing existing Alpaca credentials")
        pages, trades, tokens, identities = [], [], set(), set()
        token, previous = None, None
        for _ in range(100):
            try:
                with get(url, params={**params, **({"page_token": token} if token else {})},
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                         timeout=(15, 60), allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise MarketDataHTTPError(response.status_code)
                    if len(response.content) > 16 * 1024 * 1024:
                        raise ValueError("Trade page exceeds size budget")
                    payload = response.json()
            except requests.RequestException:
                raise RuntimeError("Market-data connection failed; credentials not logged") from None
            if (not isinstance(payload, dict) or payload.get("symbol") != symbol
                    or "trades" not in payload or not isinstance(payload["trades"], (list, type(None)))):
                raise ValueError("Unexpected trade response")
            for trade in payload.get("trades") or []:
                stamp = stamp_ns(trade["t"])
                identity = (trade["x"], trade["i"], stamp)
                conditions = trade.get("c")
                if (not low <= stamp < high or previous is not None and stamp < previous
                        or identity in identities or not isinstance(conditions, list)
                        or any(not isinstance(c, str) for c in conditions)
                        or any(type(trade[k]) not in (int, float) or not math.isfinite(trade[k]) or trade[k] <= 0 for k in ("p", "s"))):
                    raise ValueError("Invalid, duplicate or nonchronological trade")
                identities.add(identity)
                previous = stamp
                trades.append(trade)
            pages.append(payload)
            if len(trades) > 100000:
                raise ValueError("Trade minute exceeds row budget")
            token = payload.get("next_page_token")
            if not token:
                break
            if not isinstance(token, str) or token in tokens:
                raise ValueError("Repeated or invalid trade page token")
            tokens.add(token)
        else:
            raise ValueError("Trade pagination budget exceeded")
        summary = {"symbol": symbol, "start": start.isoformat(), "trades": len(trades),
                   "reported_size_sum": sum(t["s"] for t in trades), "pages": len(pages),
                   "conditions": dict(Counter(c for t in trades for c in t["c"])),
                   "finding": "no_trades_returned" if not trades else
                       "all_trades_include_odd_lot_condition" if all("I" in t["c"] for t in trades) else
                       "other_trades_require_condition_review"}
        artifact = {"version": "alpaca-trade-gap-v1", "url": url, "request": params,
                    "archived_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "responses": pages,
                    "training_ready": False, "limitations": ["Trade size sum is not condition-filtered bar volume",
                        "Empty response is provider evidence, not independent proof of no trading",
                        "No inferred OHLCV, zero fill or eligibility promotion"]}
        artifact["archive_sha256"] = hashlib.sha256(encode_dataset(artifact).encode()).hexdigest()
        fd, name = tempfile.mkstemp(dir=output, prefix=".trades_", suffix=".part")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(encode_dataset(artifact) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.link(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
        return {"file": str(path), "reused": False, **summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    result = cache_minute(args.symbol, args.start, args.output,
                         key=os.getenv("APCA_API_KEY_ID") or env.get("APCA_API_KEY_ID"),
                         secret=os.getenv("APCA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY"))
    print(encode_dataset(result))


if __name__ == "__main__":
    main()
