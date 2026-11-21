"""Join frozen retrospective news to completed local candles, without fills."""
import argparse
from collections import deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset


def _verify(data, version, digest_key):
    unsigned = deepcopy(data)
    digest = unsigned.pop(digest_key, None)
    if data.get("version") != version or digest != hashlib.sha256(encode_dataset(unsigned).encode()).hexdigest():
        raise ValueError("Unsupported or mismatched artifact digest")


def join_news(prices, news):
    version = prices.get("version")
    if version not in {"hf-historical-replay-inputs-v1", "alpaca-historical-replay-inputs-v1"}:
        raise ValueError("Unsupported historical price source")
    _verify(prices, version, "export_sha256")
    _verify(news, "retrospective-news-universe-v1", "manifest_sha256")
    symbol = prices["symbol"]
    days = news["watch_days"]
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("Invalid news watch window")
    candidates = []
    seen = set()
    for row in news["candidates"]:
        if symbol not in row["tickers"]:
            continue
        available, published = row["replay_available_ts"], row["published_ts"]
        if (type(available) is not int or type(published) is not int or not 0 < published <= available
                or not row.get("candidate_id") or row["candidate_id"] in seen):
            raise ValueError("Invalid or duplicate news candidate")
        seen.add(row["candidate_id"])
        candidates.append(row)
    candidates.sort(key=lambda r: (r["replay_available_ts"], r["candidate_id"]))
    candles = prices["candles"]
    if not candles or len(candles) > 100000:
        raise ValueError("Provide 1..100000 completed candles")
    depth = prices.get("synthetic_depth")
    books = {}
    if depth:
        _verify(depth, "synthetic-depth-v1", "scenario_sha256")
        expected = hashlib.sha256(encode_dataset(candles).encode()).hexdigest()
        if depth["input_sha256"] != expected or depth["symbol"] != symbol:
            raise ValueError("Synthetic depth does not match candle inputs")
        books = {b["source_candle_id"]: b for b in depth["snapshots"]}
        if len(books) != len(depth["snapshots"]):
            raise ValueError("Duplicate synthetic book")
    cursor, reference_count, previous_end = 0, 0, 0
    active, states, used_articles, candle_ids = deque(), [], {}, set()
    for candle in candles:
        start, end = candle["start_ts"], candle["end_ts"]
        if (type(start) is not int or type(end) is not int or not 0 < start < end
                or start < previous_end or candle["available_ts"] != end or candle["id"] in candle_ids):
            raise ValueError("Invalid completed candle sequence")
        candle_ids.add(candle["id"])
        previous_end = end
        while cursor < len(candidates) and candidates[cursor]["replay_available_ts"] <= end:
            active.append(candidates[cursor])
            cursor += 1
        while active and active[0]["replay_available_ts"] + days * 86400 <= end:
            active.popleft()
        reference_count += len(active)
        if reference_count > 250000:
            raise ValueError("News reference budget exceeded; shorten replay window")
        book = books.get(candle["id"])
        if book and (book["available_ts"] != end or not book["synthetic"]):
            raise ValueError("Synthetic book availability mismatch")
        used_articles.update({r["candidate_id"]: r for r in active})
        states.append({"decision_ts": end, "symbol": symbol, "candle_id": candle["id"],
                       "news_candidate_ids": [r["candidate_id"] for r in active],
                       "news_watch_active": bool(active),
                       "synthetic_depth_candle_id": candle["id"] if book else None,
                       "observed_price": candle["close"], "volume_source": candle["source"],
                       "scanner_inputs": {"historical_nominal_price": None, "rvol": None, "float_shares": None},
                       "scanner_eligibility": "unknown", "entry_eligible": False})
    result = {"version": "historical-news-price-join-v1", "symbol": symbol,
              "price_export_sha256": prices["export_sha256"], "news_manifest_sha256": news["manifest_sha256"],
              "watch_days": days, "news_availability_basis": news["availability_basis"],
              "news_enrichment_cutoff_ts": news["enrichment_cutoff_ts"],
              "source_file_sha256": prices["source_file_sha256"],
              "retrospective": True, "training_ready": False, "execution_supported": False,
              "live_activation_eligible": False, "network_calls": 0, "broker_calls": 0,
              "candles": candles, "synthetic_depth": depth, "articles": list(used_articles.values()),
              "states": states, "state_count": len(states),
              "news_active_states": sum(s["news_watch_active"] for s in states),
              "limitations": list(prices.get("limitations", [])) + [news["hindsight_risk"],
                 "News watch activity is not Scanner admission or trade permission",
                 "Nominal price, historical RVOL and float eligibility remain unresolved"]}
    if prices.get("source_archives"):
        result["source_archives"] = deepcopy(prices["source_archives"])
    if "requested_start_ts" in prices:
        result["requested_start_ts"] = prices["requested_start_ts"]
    if news.get("source_shards"):
        result["source_news_shards"] = list(news["source_shards"])
    result["join_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--news", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = join_news(json.loads(args.prices.read_text()), json.loads(args.news.read_text()))
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({k: v for k, v in result.items() if k not in {"states", "candles", "synthetic_depth", "articles"}}))


if __name__ == "__main__":
    main()
