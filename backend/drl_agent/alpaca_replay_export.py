"""Local Alpaca SIP/raw cache to nominal completed candles and synthetic depth."""
import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from drl_agent.alpaca_data_cache import URL, _time
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify
from drl_agent.synthetic_depth import DepthConfig, generate_depth


def build_export(archive, *, depth_config=None):
    _verify(archive, "alpaca-sip-raw-cache-v1", "archive_sha256")
    request = archive["request"]
    if (archive["url"] != URL or request["feed"] != "sip" or request["adjustment"] != "raw"
            or request["timeframe"] != "1Min" or request["asof"] != "-"):
        raise ValueError("Require SIP/raw minute cache with historical ticker mapping disabled")
    symbol = request["symbols"]
    start, end = _time(request["start"]), _time(request["end"])
    candles, previous = [], None
    for page in archive["responses"]:
        if set(page.get("bars") or {}) - {symbol}:
            raise ValueError("Unexpected symbol")
        for bar in (page.get("bars") or {}).get(symbol, []):
            stamp = _time(bar["t"])
            values = [bar[k] for k in ("o", "h", "l", "c", "v")]
            if (not start <= stamp <= end or stamp.second or stamp.microsecond
                    or (previous is not None and stamp <= previous)
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                    or min(values[:4]) <= 0 or values[4] < 0
                    or bar["h"] < max(values[:4]) or bar["l"] > min(values[:4])):
                raise ValueError("Invalid or nonchronological Alpaca bar")
            previous = stamp
            ts = int(stamp.timestamp())
            identity = hashlib.sha256(encode_dataset([archive["archive_sha256"], ts]).encode()).hexdigest()
            candles.append({"id": identity, "start_ts": ts, "end_ts": ts + 60, "available_ts": ts + 60,
                "open": bar["o"], "high": bar["h"], "low": bar["l"], "close": bar["c"],
                "volume": bar["v"], "source": "alpaca_sip", "price_basis": "nominal", "retrospective": True})
    if not 1 <= len(candles) <= 100000 or len(candles) != archive["summary"]["rows"]:
        raise ValueError("Invalid candle count")
    result = {"version": "alpaca-historical-replay-inputs-v1", "symbol": symbol,
        "source_file_sha256": archive["archive_sha256"], "source_archived_at_utc": archive["archived_at"],
        "price_basis": "nominal", "availability_basis": "bar_start_plus_60_seconds_assumption",
        "candles": candles, "source_rows": {"alpaca_sip": len(candles)}, "rejected_rows": {},
        "requested_start_ts": int(start.timestamp()),
        "retrospective": True, "training_ready": False, "execution_supported": False,
        "live_activation_eligible": False, "broker_calls": 0, "network_calls": 0,
        "limitations": ["SIP minute bars are not recorded order-book depth",
            "Historical bar availability assumes completion; publication/correction delays are not known",
            "No missing minutes are invented; actual coverage requires session checks",
            "Unadjusted prices can jump on corporate actions; no split/dividend accounting is inferred"]}
    if depth_config is not None:
        result["synthetic_depth"] = generate_depth(symbol, candles, config=depth_config)
    result["export_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def merge_exports(exports):
    """Keep overlapping identical minutes once; conflicting revisions require review."""
    if not exports:
        raise ValueError("Require price exports")
    sources, candles = {}, {}
    symbol = exports[0]["symbol"]
    for exported in exports:
        _verify(exported, "alpaca-historical-replay-inputs-v1", "export_sha256")
        if exported["symbol"] != symbol or exported.get("price_basis") != "nominal":
            raise ValueError("Cannot mix symbols or price bases")
        source = exported["source_file_sha256"]
        sources[source] = exported["source_archived_at_utc"]
        for candle in exported["candles"]:
            stamp = candle["start_ts"]
            content = {k: v for k, v in candle.items() if k != "id"}
            if stamp in candles and candles[stamp] != content:
                raise ValueError("Conflicting overlapping candle revisions")
            candles[stamp] = content
    if len(candles) > 100000:
        raise ValueError("Merged export exceeds candle budget")
    result = deepcopy(exports[0])
    result.pop("synthetic_depth", None)
    result["source_archives"] = [{"archive_sha256": s, "archived_at": sources[s]} for s in sorted(sources)]
    result["source_file_sha256"] = hashlib.sha256(encode_dataset(result["source_archives"]).encode()).hexdigest()
    result["source_archived_at_utc"] = max(sources.values())
    if all("requested_start_ts" in e for e in exports):
        result["requested_start_ts"] = min(e["requested_start_ts"] for e in exports)
    else:
        result.pop("requested_start_ts", None)
    result["candles"] = [{**candles[t], "id": hashlib.sha256(encode_dataset(["alpaca_sip", symbol, t]).encode()).hexdigest()}
                         for t in sorted(candles)]
    result["source_rows"] = {"alpaca_sip": len(candles)}
    result["export_sha256"] = hashlib.sha256(encode_dataset({k: v for k, v in result.items() if k != "export_sha256"}).encode()).hexdigest()
    return result


def slice_export(exported, start_ts, end_ts, *, depth_config=None):
    _verify(exported, "alpaca-historical-replay-inputs-v1", "export_sha256")
    if not start_ts < end_ts:
        raise ValueError("Invalid export slice")
    result = deepcopy(exported)
    result.pop("synthetic_depth", None)
    if "requested_start_ts" in result:
        result["requested_start_ts"] = max(start_ts, result["requested_start_ts"])
    result["candles"] = [c for c in result["candles"] if start_ts <= c["start_ts"] and c["end_ts"] <= end_ts]
    if not result["candles"]:
        raise ValueError("No candles in export slice")
    result["source_rows"] = {"alpaca_sip": len(result["candles"])}
    if depth_config is not None:
        result["synthetic_depth"] = generate_depth(result["symbol"], result["candles"], config=depth_config)
    result["export_sha256"] = hashlib.sha256(encode_dataset({k: v for k, v in result.items() if k != "export_sha256"}).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic-depth", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = build_export(json.loads(args.archive.read_text()),
                          depth_config=DepthConfig() if args.synthetic_depth else None)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({"symbol": result["symbol"], "candles": len(result["candles"]),
                         "source": "alpaca_sip", "price_basis": "nominal", "export_sha256": result["export_sha256"]}))


if __name__ == "__main__":
    main()
