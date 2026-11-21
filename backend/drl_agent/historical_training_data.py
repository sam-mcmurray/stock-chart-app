"""Validate and partition local historical/synthetic training session manifests."""
from collections import Counter
from datetime import date
import hashlib
import json

from drl_agent.dataset import encode_dataset
from drl_agent.historical_learning import FEATURE_NAMES, HistoricalLearningReplay
from drl_agent.historical_watch import build_watch
from drl_agent.watch_replay import WatchConfig

VERSION = "historical-training-partitions-v1"


def prepare_historical(path, train_end, validation_end, embargo_seconds, *, require_coverage=True):
    if date.fromisoformat(train_end) >= date.fromisoformat(validation_end):
        raise ValueError("Training boundary must precede validation boundary")
    if type(embargo_seconds) is not int or not 0 <= embargo_seconds <= 604800:
        raise ValueError("Embargo must be 0..604800 seconds")
    manifest = json.loads(path.read_text())
    if manifest.get("version") != "historical-training-manifest-v1":
        raise ValueError("Unsupported historical training manifest")
    raw_sessions = manifest["sessions"]
    if not 1 <= len(raw_sessions) <= 1000:
        raise ValueError("Provide 1..1000 sessions")
    config = WatchConfig(**manifest["watch_config"])
    research_policy = manifest.get("research_policy", {})
    cache, watches, inspected, sessions = {}, {}, [], []
    def load(name):
        file = (path.parent / name).resolve()
        if file not in cache:
            cache[file] = json.loads(file.read_text())
        return cache[file]
    for index, raw in enumerate(raw_sessions):
        day = date.fromisoformat(raw["date"]).isoformat()
        if inspected and day <= inspected[-1]["date"]:
            raise ValueError("Sessions must be distinct dates in chronological order")
        joined = [load(name) for name in raw["joined"]]
        features = [load(name) for name in raw["features"]]
        evidence = load(raw["evidence"]) if raw.get("evidence") else []
        if not isinstance(evidence, list):
            raise ValueError("Evidence must be an array of point-in-time measurements")
        key = (tuple(raw["joined"]), tuple(raw["features"]), raw.get("evidence"))
        if key not in watches:
            # Recompute readiness from measured features; never accept a caller's
            # premarked ready flag or an older pre-staged watch artifact.
            watches[key] = build_watch(joined, features, evidence, config=config, **research_policy)
        watch = watches[key]
        session = {"date": day, "joined": joined, "features": features, "watch": watch}
        replay = HistoricalLearningReplay(session)
        frames = {frame["decision_ts"]: frame for frame in watch["frames"]}
        refs, blockers, watch_ticks, ready_pairs = set(), Counter(), 0, 0
        first_input = replay.times[0]
        for i, timestamp in enumerate(replay.times):
            candidates = frames[timestamp]["watching"]
            watch_ticks += len(candidates)
            blockers.update(reason for c in candidates for reason in c["entry_block_reasons"])
            blockers.update(reason for reasons in frames[timestamp]["rejected"].values() for reason in reasons)
            for symbol, sim in replay.engine.simulators.items():
                state = sim.states[i]
                refs.add(("candle", symbol, state["candle_id"]))
                for identity in state["news_candidate_ids"]:
                    refs.add(("news", identity))
                    first_input = min(first_input, replay.engine.articles[symbol][identity]["replay_available_ts"])
            if i + 1 < len(replay.times) and replay.times[i + 1] - timestamp == 60:
                next_ready = {c["symbol"] for c in frames[replay.times[i + 1]]["watching"]
                              if c["research_entry_ready"]}
                ready_pairs += sum(c["research_entry_ready"] and c["symbol"] in next_ready for c in candidates)
        inspected.append({"date": day, "session_index": index, "start_ts": replay.times[0],
                          "end_ts": replay.times[-1], "watch_symbol_ticks": watch_ticks,
                          "eligible_entry_transitions": ready_pairs, "entry_block_counts": dict(blockers),
                          "source_hashes": replay.source_hashes, "watch_sha256": watch["watch_sha256"],
                          "split": "train" if day <= train_end else "validation" if day <= validation_end else "test",
                          "_references": refs, "_first_input": first_input})
        sessions.append(session)
    names = ("train", "validation", "test")
    partitions = {name: [] for name in names}
    groups = {name: [] for name in names}
    purged = []
    for row in inspected:
        later = [other for other in inspected if names.index(other["split"]) > names.index(row["split"])]
        reasons = []
        if any(row["_references"] & other["_references"] for other in later):
            reasons.append("shared_news_or_session_candles")
        if any(row["end_ts"] + embargo_seconds >= other["_first_input"] for other in later):
            reasons.append("news_or_session_interval_embargo_overlap")
        public = {key: value for key, value in row.items() if not key.startswith("_")}
        if reasons:
            purged.append({**public, "reasons": reasons})
        else:
            partitions[row["split"]].append(public)
            groups[row["split"]].append(sessions[row["session_index"]])
    coverage = {name: {"sessions": len(rows),
                      "watch_symbol_ticks": sum(row["watch_symbol_ticks"] for row in rows),
                      "eligible_entry_transitions": sum(row["eligible_entry_transitions"] for row in rows)}
                for name, rows in partitions.items()}
    blocked = [name + (":empty_after_purge" if not counts["sessions"] else ":no_eligible_entry_transitions")
               for name, counts in coverage.items() if not counts["sessions"] or not counts["eligible_entry_transitions"]]
    report = {"version": VERSION, "synthetic": True, "retrospective": True,
              "train_end": train_end, "validation_end": validation_end, "embargo_seconds": embargo_seconds,
              "watch_config": manifest["watch_config"], "research_policy": research_policy,
              "partitions": partitions, "purged": purged, "coverage": coverage,
              "coverage_blockers": blocked, "basic_coverage_passed": not blocked,
              "training_ready": False, "promotion_allowed": False, "broker_calls": 0,
              "limitations": ["Eligibility coverage is not a promise of executable fills",
                              "Synthetic minute-close liquidity; retrospective article enrichment",
                              "Purging covers shared articles and session candles, not every rolling indicator warmup bar"]}
    report["partition_sha256"] = hashlib.sha256(encode_dataset(report).encode()).hexdigest()
    if require_coverage and blocked:
        raise ValueError("Historical training data blocked: " + ", ".join(blocked))
    return groups, config, report, FEATURE_NAMES
