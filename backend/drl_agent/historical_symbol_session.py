"""Build one auditable daily session from cached symbol windows and news shards."""
import argparse
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from drl_agent.alpaca_replay_export import build_export, merge_exports, slice_export
from drl_agent.dataset import encode_dataset
from drl_agent.historical_features import build_features
from drl_agent.historical_news_join import _verify, join_news
from drl_agent.synthetic_depth import DepthConfig


def build_session(archives, shards, session_date, *, trade_evidence=()):
    prices = merge_exports([build_export(a) for a in archives])
    if not shards:
        raise ValueError("Require frozen news shards")
    news = deepcopy(shards[0])
    candidates, hashes = {}, set()
    for shard in shards:
        _verify(shard, "retrospective-news-universe-v1", "manifest_sha256")
        if any(shard[k] != news[k] for k in ("watch_days", "processing_delay_seconds", "enrichment_cutoff_ts", "availability_basis")):
            raise ValueError("Mixed news configurations")
        hashes.add(shard["manifest_sha256"])
        for row in shard["candidates"]:
            if prices["symbol"] not in row["tickers"]:
                continue
            old = candidates.get(row["candidate_id"])
            if old is not None and old != row:
                raise ValueError("Conflicting news candidate")
            candidates[row["candidate_id"]] = row
    news.update(candidates=sorted(candidates.values(), key=lambda r: (r["replay_available_ts"], r["candidate_id"])),
                selected_article_count=len(candidates), source_shards=sorted(hashes), price_requests=[],
                publication_window={"start_ts": min(s["publication_window"]["start_ts"] for s in shards),
                                    "end_ts": max(s["publication_window"]["end_ts"] for s in shards)})
    news.pop("manifest_sha256")
    news["manifest_sha256"] = hashlib.sha256(encode_dataset(news).encode()).hexdigest()
    day = date.fromisoformat(session_date)
    start = int(datetime.combine(day, time(), ZoneInfo("America/New_York")).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), time(), ZoneInfo("America/New_York")).timestamp())
    daily = slice_export(prices, start, end, depth_config=DepthConfig())
    joined = join_news(daily, news)
    first = prices.get("requested_start_ts", prices["candles"][0]["start_ts"])
    warmup = [slice_export(prices, first, start)] if prices["candles"][0]["start_ts"] < start else []
    if not warmup and first < start:
        joined["requested_start_ts"] = first
        joined.pop("join_sha256")
        joined["join_sha256"] = hashlib.sha256(encode_dataset(joined).encode()).hexdigest()
    features = build_features(joined, warmup=warmup, trade_evidence=trade_evidence)
    return joined, features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="append", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trade-evidence", action="append", type=Path, default=[])
    parser.add_argument("--watch-config", type=Path, help="Optional existing watch_config for strict entry diagnostics")
    args = parser.parse_args()
    progress = json.loads((args.corpus / "progress.json").read_text())
    if progress["status"] != "complete":
        raise ValueError("Require complete news corpus")
    shards = []
    for record in progress["completed"]:
        path = Path(record["file"])
        if path.name != str(path):
            raise ValueError("Invalid shard filename")
        shard = json.loads((args.corpus / path).read_text())
        if shard["manifest_sha256"] != record["manifest_sha256"]:
            raise ValueError("Changed corpus shard")
        shards.append(shard)
    joined, features = build_session([json.loads(p.read_text()) for p in args.archive], shards, args.date,
                                     trade_evidence=[json.loads(p.read_text()) for p in args.trade_evidence])
    watch = None
    if args.watch_config:
        from drl_agent.historical_watch import build_watch
        from drl_agent.watch_replay import WatchConfig
        watch = build_watch([joined], [features], [],
            config=WatchConfig(**json.loads(args.watch_config.read_text())["watch_config"]))
    args.output.mkdir(parents=True, exist_ok=False)
    for name, data in (("joined.json", joined), ("features.json", features)):
        (args.output / name).write_text(encode_dataset(data) + "\n")
    report = {"symbol": joined["symbol"], "date": args.date, "news_active_states": joined["news_active_states"],
              "articles": len(joined["articles"]), "coverage": features["coverage"], "warmup": features.get("warmup"),
              "excluded_partial_request_sessions": features.get("excluded_partial_request_sessions", []),
              "training_started": False, "float_evidence_supplied": False, "network_calls": 0, "broker_calls": 0}
    if watch is not None:
        (args.output / "watch.json").write_text(encode_dataset(watch) + "\n")
        candidates = [c for frame in watch["frames"] for c in frame["watching"]]
        report.update(watch_sha256=watch["watch_sha256"], watched_states=len(candidates),
                      momentum_states=sum(c["momentum_triggered"] for c in candidates),
                      entry_ready_states=sum(c["research_entry_ready"] for c in candidates),
                      entry_block_counts=watch["entry_block_counts"], rejection_counts=watch["rejection_counts"])
    (args.output / "summary.json").write_text(encode_dataset(report) + "\n")
    print(encode_dataset(report))


if __name__ == "__main__":
    main()
