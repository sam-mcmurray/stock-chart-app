"""Resumable daily exports of verified historical news; no market/broker calls."""
import argparse
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import json
from pathlib import Path

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news import build_universe, load_versions
from drl_agent.historical_news_join import _verify

VERSION = "historical-news-corpus-v1"


def _save(path, payload):
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(encode_dataset(payload) + "\n", encoding="utf-8")
    pending.replace(path)


def export_corpus(output, *, start, end, enrichment_cutoff_ts, max_days=7,
                  loader=load_versions, notify=None, price_lookback_days=None):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first >= last or (last - first).days > 18263 or type(max_days) is not int or not 1 <= max_days <= 366:
        raise ValueError("Require increasing dates (at most 50 years) and max_days 1..366")
    if type(enrichment_cutoff_ts) is not int or enrichment_cutoff_ts < int(datetime.combine(last, time(), timezone.utc).timestamp()):
        raise ValueError("Freeze enrichment cutoff at or after the publication window")
    config = {"version": VERSION, "start": start, "end": end,
              "enrichment_cutoff_ts": enrichment_cutoff_ts,
              "processing_delay_seconds": 60, "watch_days": 7}
    if price_lookback_days is not None and (type(price_lookback_days) is not int or not 1 <= price_lookback_days <= 365):
        raise ValueError("price_lookback_days must be an integer from 1 to 365")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".export.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another exporter owns this corpus") from None
        config_path = output / "config.json"
        frozen = json.loads(config_path.read_text()) if config_path.exists() else None
        # Resume the original acquisition window; only new corpora get the larger buffer.
        lookback = price_lookback_days if price_lookback_days is not None else (
            frozen["price_lookback_days"] if frozen is not None else 45)
        if type(lookback) is not int or not 1 <= lookback <= 365:
            raise ValueError("Invalid frozen price lookback")
        config["price_lookback_days"] = lookback
        if config_path.exists():
            if frozen != config:
                raise ValueError("Frozen corpus configuration changed; use a new output directory")
        else:
            _save(config_path, config)
        progress = {**config, "status": "running", "completed": [], "selected_articles": 0,
                    "price_requests": 0, "unique_symbols": 0, "exported_this_run": 0,
                    "training_started": False, "broker_calls": 0}
        symbols = set()
        day = first
        while day < last:
            path = output / (day.isoformat() + ".json")
            if not path.exists() and progress["exported_this_run"] >= max_days:
                break
            start_ts = int(datetime.combine(day, time(), timezone.utc).timestamp())
            options = {"start_ts": start_ts, "end_ts": start_ts + 86400,
                       "enrichment_cutoff_ts": enrichment_cutoff_ts}
            progress.update(current_day=day.isoformat(), updated_at=datetime.now(timezone.utc).isoformat())
            _save(output / "progress.json", progress)
            try:
                if path.exists():
                    universe = json.loads(path.read_text())
                    _verify(universe, "retrospective-news-universe-v1", "manifest_sha256")
                    if (universe["publication_window"] != {"start_ts": start_ts, "end_ts": start_ts + 86400}
                            or universe["enrichment_cutoff_ts"] != enrichment_cutoff_ts
                            or any(universe[key] != config[key] for key in
                                   ("processing_delay_seconds", "watch_days", "price_lookback_days"))):
                        raise ValueError("Existing shard does not match frozen corpus")
                else:
                    universe = build_universe(loader(**options), **options, price_lookback_days=lookback)
                    _save(path, universe)
                    progress["exported_this_run"] += 1
            except Exception as exc:
                progress.update(status="failed", error_type=type(exc).__name__)
                _save(output / "progress.json", progress)
                raise
            symbols.update(symbol for row in universe["candidates"] for symbol in row["tickers"])
            progress["selected_articles"] += universe["selected_article_count"]
            progress["price_requests"] += len(universe["price_requests"])
            progress["unique_symbols"] = len(symbols)
            progress["completed"].append({"date": day.isoformat(), "file": path.name,
                "manifest_sha256": universe["manifest_sha256"], "articles": universe["selected_article_count"],
                "rejections": universe["rejections"]})
            if notify:
                notify({"date": day.isoformat(), "completed_days": len(progress["completed"]),
                        "selected_articles": progress["selected_articles"], "unique_symbols": len(symbols)})
            day += timedelta(days=1)
        progress.update(status="complete" if day == last else "paused_at_batch_limit",
                        next_day=day.isoformat() if day < last else None,
                        updated_at=datetime.now(timezone.utc).isoformat())
        _save(output / "progress.json", progress)
        return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="First UTC publication date, inclusive")
    parser.add_argument("--end", required=True, help="Last UTC publication boundary, exclusive")
    parser.add_argument("--enrichment-cutoff-ts", type=int, required=True)
    parser.add_argument("--max-days", type=int, default=7)
    parser.add_argument("--price-lookback-days", type=int,
                        help="Calendar-day history buffer; new corpora default to 45, resumed corpora keep their value")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = export_corpus(**vars(args), notify=lambda row: print(encode_dataset(row), flush=True))
    print(encode_dataset({key: value for key, value in report.items() if key != "completed"}), flush=True)


if __name__ == "__main__":
    main()
