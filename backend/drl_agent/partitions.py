"""Chronological, whole-session partitions and descriptive replay coverage."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
import hashlib
from zoneinfo import ZoneInfo

from drl_agent.dataset import _normalize, encode_dataset
from drl_agent.portfolio_replay import PortfolioReplay
from drl_agent.simulator import Action

VERSION = "drl-partitions-v1"
SPLITS = ("train", "validation", "test")


def inspect_session(datasets, watch_config):
    for dataset in datasets:
        unsigned = deepcopy(dataset)
        claimed = unsigned.get("manifest", {}).pop("dataset_sha256", None)
        actual = hashlib.sha256(encode_dataset(unsigned).encode()).hexdigest()
        if claimed != actual:
            raise ValueError("Missing or mismatched observation dataset digest")
    replay = PortfolioReplay(datasets, watch_config=watch_config)
    references = set()
    first_input = replay.times[0] * 1000
    market_coverage = {}
    watch_market_coverage = {}
    watched = [{c["symbol"] for c in frame["watching"]} for frame in replay.watch["frames"]]
    for symbol, sim in replay.simulators.items():
        counts = {"states": len(sim.states), "usable_quote_states": 0,
                  "present_quote_states": 0, "present_book_states": 0,
                  "fresh_valid_book_states": 0, "present_candle_states": 0,
                  "fresh_candle_states": 0, "fresh_scanner_states": 0}
        watch_counts = dict.fromkeys(counts, 0)
        for index, (state, features) in enumerate(zip(sim.states, sim.features)):
            before = counts.copy()
            if features["rejected_inputs"]:
                raise ValueError("Dataset contains invalid or unavailable observation inputs")
            values = dict(zip(features["names"], features["values"]))
            counts["usable_quote_states"] += int(sim._quote(index) is not None)
            for counter, field in (("present_quote_states", "levelone_quote.present"),
                                   ("present_book_states", "level2_book.present"),
                                   ("present_candle_states", "minute_candle.present"),
                                   ("fresh_candle_states", "minute_candle.fresh"),
                                   ("fresh_scanner_states", "scanner.fresh")):
                counts[counter] += int(values[field] == 1)
            counts["fresh_valid_book_states"] += int(values["level2_book.fresh"] == 1 and values["book.valid"] == 1)
            if symbol in watched[index]:
                watch_counts["states"] += 1
                for key in counts:
                    if key != "states":
                        watch_counts[key] += counts[key] - before[key]
            for kind, items in state["inputs"].items():
                if kind not in {"scanner", "levelone_quote", "level2_book", "minute_candle"}:
                    raise ValueError("Unsupported input family")
                for item in items:
                    row = _normalize(item["record"], scanner=kind == "scanner")
                    first_input = min(first_input, row["event_ms"])
                    references.add((kind, symbol, row["service"], row["id"]))
                    if kind == "scanner":
                        hype = (row["record"].get("payload", {}).get("semantic_signal") or {}).get("hype") or {}
                        for identity in hype.get("evidence_article_ids") or []:
                            if isinstance(identity, str) and identity.strip():
                                # An article shared across companies is still the
                                # same evidence and must not cross partitions.
                                references.add(("news", identity.strip()))
        market_coverage[symbol] = counts
        watch_market_coverage[symbol] = watch_counts
    frames = replay.watch["frames"]
    rejection_counts = {}
    for frame in frames:
        for reason in frame["rejected"].values():
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    probes, fills = 0, 0
    probe_rejections = {}
    for i, frame in enumerate(frames[:-1]):
        next_watch = {c["symbol"] for c in frames[i + 1]["watching"] if not c["entry_block_reasons"]}
        for candidate in frame["watching"]:
            if candidate["symbol"] not in next_watch or candidate["entry_block_reasons"]:
                continue
            sim = replay.simulators[candidate["symbol"]]
            sim.reset()
            sim.index = i
            *_, event = sim.step(Action.ENTER)
            probes += 1
            fills += int(event["fill"] is not None)
            if event["fill"] is None:
                reason = event["reason"]
                probe_rejections[reason] = probe_rejections.get(reason, 0) + 1
    session_date = datetime.fromtimestamp(replay.times[0], ZoneInfo("America/New_York")).date()
    return {"date": session_date.isoformat(), "start_ts": replay.times[0], "end_ts": replay.times[-1],
            "symbols": sorted(replay.simulators), "source_hashes": replay.source_hashes,
            "state_count": sum(len(s.states) for s in replay.simulators.values()),
            "watch_symbol_ticks": sum(len(f["watching"]) for f in frames),
            "watch_rejections": rejection_counts, "entry_probes": probes,
            "fillable_entry_probes": fills, "entry_probe_rejections": probe_rejections,
            "market_coverage": market_coverage, "watch_market_coverage": watch_market_coverage,
            "_references": references,
            "_first_input_ms": first_input}


def partition_sessions(sessions, *, watch_config, train_end, validation_end, embargo_seconds=86400):
    """Dates are explicit, inclusive NY session boundaries; never tune on P/L.

    Later partitions retain priority. Earlier whole sessions are purged when
    they share source records/news with any later partition or their observation
    interval lies too close to a later partition's input history.
    """
    train_date, validation_date = date.fromisoformat(train_end), date.fromisoformat(validation_end)
    if train_date >= validation_date:
        raise ValueError("Training boundary must precede validation boundary")
    train_end, validation_end = train_date.isoformat(), validation_date.isoformat()
    if isinstance(embargo_seconds, bool) or not isinstance(embargo_seconds, int) or not 0 <= embargo_seconds <= 604800:
        raise ValueError("Embargo must be 0..604800 seconds")
    if not sessions or len(sessions) > 1000:
        raise ValueError("Provide 1..1000 sessions per manifest")
    inspected = []
    for index, datasets in enumerate(sessions):
        row = inspect_session(datasets, watch_config)
        if inspected and row["date"] <= inspected[-1]["date"]:
            raise ValueError("Provide distinct session dates in chronological order")
        row["session_index"] = index
        row["split"] = "train" if row["date"] <= train_end else "validation" if row["date"] <= validation_end else "test"
        inspected.append(row)
    partitions = {name: [] for name in SPLITS}
    purged = []
    for row in inspected:
        later = [r for r in inspected if SPLITS.index(r["split"]) > SPLITS.index(row["split"])]
        reasons = []
        if any(row["_references"] & other["_references"] for other in later):
            reasons.append("shared_news_or_market_evidence")
        if any(row["end_ts"] * 1000 + embargo_seconds * 1000 >= other["_first_input_ms"] for other in later):
            reasons.append("observation_interval_or_embargo_overlap")
        public = {k: v for k, v in row.items() if not k.startswith("_")}
        if reasons:
            purged.append({**public, "reasons": reasons})
        else:
            partitions[row["split"]].append(public)
    coverage = {}
    blockers = []
    for name, rows in partitions.items():
        coverage[name] = {"sessions": len(rows), "states": sum(r["state_count"] for r in rows),
                          "watch_symbol_ticks": sum(r["watch_symbol_ticks"] for r in rows),
                          "fillable_entry_probes": sum(r["fillable_entry_probes"] for r in rows)}
        if not rows:
            blockers.append(name + ":empty_after_purge")
        elif not coverage[name]["fillable_entry_probes"]:
            blockers.append(name + ":no_fillable_watch_candidates")
    result = {"version": VERSION, "train_end": train_end, "validation_end": validation_end,
              "watch_config": asdict(watch_config),
              "embargo_seconds": embargo_seconds, "partitions": partitions, "purged": purged,
              "coverage": coverage, "coverage_blockers": blockers,
              "basic_coverage_passed": not blockers, "training_ready": False,
              "promotion_allowed": False, "broker_calls": 0}
    result["partition_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result
