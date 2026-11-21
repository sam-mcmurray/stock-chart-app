"""Plan missing market-data acquisitions from frozen news and existing raw caches."""
import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path

from drl_agent.alpaca_data_cache import _time
from drl_agent.alpaca_replay_export import build_export
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def merge(windows):
    result = []
    for start, end in sorted(windows):
        if start >= end:
            raise ValueError("Invalid acquisition window")
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def subtract(wanted, cached):
    result = []
    for start, end in merge(wanted):
        cursor = start
        for low, high in merge(cached):
            if high <= cursor:
                continue
            if low >= end:
                break
            if low > cursor:
                result.append((cursor, min(low, end)))
            cursor = max(cursor, high)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def plan(corpus, cache):
    corpus, cache = Path(corpus), Path(cache)
    config = json.loads((corpus / "config.json").read_text())
    progress = json.loads((corpus / "progress.json").read_text())
    if (config.get("version") != "historical-news-corpus-v1" or progress.get("status") != "complete"
            or any(progress.get(k) != v for k, v in config.items())):
        raise ValueError("Require a completed frozen news corpus")
    first, last = date.fromisoformat(config["start"]), date.fromisoformat(config["end"])
    expected = {(first + timedelta(days=i)).isoformat() + ".json" for i in range((last - first).days)}
    if not expected or {row["file"] for row in progress["completed"]} != expected:
        raise ValueError("Corpus has missing or unexpected daily shards")
    wanted, cached, archives, sources = defaultdict(list), defaultdict(list), [], []
    seen = set()
    for row in progress["completed"]:
        name = row["file"]
        if Path(name).name != name or name in seen:
            raise ValueError("Invalid or duplicate corpus shard")
        seen.add(name)
        shard = json.loads((corpus / name).read_text())
        _verify(shard, "retrospective-news-universe-v1", "manifest_sha256")
        if shard["manifest_sha256"] != row["manifest_sha256"] or shard["enrichment_cutoff_ts"] != config["enrichment_cutoff_ts"]:
            raise ValueError("Corpus shard identity mismatch")
        start_ts = int(datetime.combine(date.fromisoformat(name[:-5]), time(), timezone.utc).timestamp())
        if shard["publication_window"] != {"start_ts": start_ts, "end_ts": start_ts + 86400}:
            raise ValueError("Corpus shard publication window mismatch")
        sources.append(shard["manifest_sha256"])
        for request in shard["price_requests"]:
            wanted[request["symbol"]].append((request["start_ts"] // 60 * 60,
                                               (request["end_ts"] + 59) // 60 * 60))
    for path in sorted(cache.glob("*_sip_raw.json")):
        archive = json.loads(path.read_text())
        symbol = archive["request"]["symbols"]
        if symbol not in wanted:
            continue
        exported = build_export(archive)
        start = int(_time(archive["request"]["start"]).timestamp())
        end = int((_time(archive["request"]["end"]) + timedelta(seconds=1)).timestamp())
        if start % 60 or end % 60:
            raise ValueError("Cached request bounds must be minute-aligned")
        cached[symbol].append((start, end))
        archives.append({"file": path.name, "symbol": symbol, "archive_sha256": archive["archive_sha256"],
                         "start_ts": start, "end_ts": end, "returned_bars": len(exported["candles"])})
    requests = []
    for symbol, intervals in sorted(wanted.items()):
        for start, end in subtract(intervals, cached[symbol]):
            while start < end:
                stop = min(end, start + 31 * 86400)
                identity = hashlib.sha256(encode_dataset([symbol, start, stop, "sip", "raw"]).encode()).hexdigest()
                requests.append({"request_id": identity, "symbol": symbol, "start_ts": start,
                                 "end_ts": stop, "status": "acquisition_required"})
                start = stop
    result = {"version": "historical-corpus-price-plan-v1", "source_shards": sources,
              "enrichment_cutoff_ts": config["enrichment_cutoff_ts"],
              "symbols": len(wanted), "daily_requirements": sum(len(v) for v in wanted.values()),
              "merged_windows": sum(len(merge(v)) for v in wanted.values()),
              "reusable_archives": archives, "requests": requests,
              "network_calls": 0, "broker_calls": 0, "training_started": False,
              "limitations": ["Cached request coverage is not certified minute-bar completeness",
                              "Absent provider bars are not filled or silently requested again",
                              "Price acquisition plan does not establish historical float or entry eligibility",
                              "No screening by future return or today's price"]}
    result["plan_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    report = plan(args.corpus, args.cache)
    with args.output.open("x") as stream:
        stream.write(encode_dataset(report) + "\n")
    print(encode_dataset({"symbols": report["symbols"], "daily_requirements": report["daily_requirements"],
                          "merged_windows": report["merged_windows"], "reusable_archives": len(report["reusable_archives"]),
                          "missing_requests": len(report["requests"]), "network_calls": 0}))


if __name__ == "__main__":
    main()
