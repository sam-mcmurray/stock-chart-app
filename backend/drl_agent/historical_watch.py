"""Point-in-time research watch admission; unknown evidence never qualifies."""
from collections import Counter
from dataclasses import asdict
import hashlib
import math

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def prepare_evidence(evidence, symbols):
    if not isinstance(evidence, (list, tuple)) or len(evidence) > 100000:
        raise ValueError("Invalid or excessive watch evidence")
    prepared, identities = [], set()
    for raw in evidence:
        if not isinstance(raw, dict):
            raise ValueError("Invalid point-in-time evidence")
        row = dict(raw)
        symbol, kind = row.get("symbol"), row.get("kind")
        if not isinstance(symbol, str) or not isinstance(kind, str) or symbol not in symbols or kind not in {"price", "float"}:
            raise ValueError("Invalid evidence symbol or kind")
        times = [row.get(k) for k in ("effective_ts", "available_ts", "valid_until_ts")]
        value = row.get("value")
        if (any(type(t) is not int or t <= 0 for t in times) or not times[0] <= times[1] < times[2]
                or type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                or row.get("basis") not in ({"nominal"} if kind == "price" else
                                           {"float_shares", "sec_reported_nonaffiliate_proxy"})
                or not isinstance(row.get("source"), str) or not row["source"].strip()):
            raise ValueError("Invalid point-in-time evidence")
        identity = (symbol, kind, times[0], times[1])
        if identity in identities:
            raise ValueError("Ambiguous duplicate evidence")
        identities.add(identity)
        row["evidence_id"] = hashlib.sha256(encode_dataset(raw).encode()).hexdigest()
        prepared.append(row)
    return sorted(prepared, key=lambda r: (r["available_ts"], r["effective_ts"], r["symbol"], r["kind"]))


def build_watch(datasets, features, evidence, *, config, allow_adjusted_price=False, allow_venue_rvol=False,
                allow_partial_volume_rvol=False, allow_sec_float_proxy=False):
    """Evidence: symbol, kind (price/float), value, basis, source, effective_ts,
    available_ts and valid_until_ts. Sources are caller-supplied provenance,
    not independently authenticated by this builder. Future evidence is withheld.
    """
    if any(type(v) is not bool for v in (allow_adjusted_price, allow_venue_rvol, allow_partial_volume_rvol,
                                       allow_sec_float_proxy)):
        raise ValueError("Research options must be boolean")
    if not datasets or len(evidence) > 100000 or sum(len(d["states"]) for d in datasets) > 100000:
        raise ValueError("Invalid or excessive watch inputs")
    by_symbol, grids, feature_hashes = {}, None, {}
    for artifact in features:
        _verify(artifact, "historical-bar-features-v2", "features_sha256")
        if artifact["symbol"] in by_symbol:
            raise ValueError("Duplicate feature symbol")
        by_symbol[artifact["symbol"]] = artifact
    sources, inputs = {}, {}
    for joined in datasets:
        _verify(joined, "historical-news-price-join-v1", "join_sha256")
        symbol = joined["symbol"]
        if symbol in sources:
            raise ValueError("Duplicate joined symbol")
        feature = by_symbol.get(symbol)
        if not feature or feature["source_join_sha256"] != joined["join_sha256"]:
            raise ValueError("Features must match joined artifact")
        times = [s["decision_ts"] for s in joined["states"]]
        if not times or any(a >= b for a, b in zip(times, times[1:])) or (grids is not None and times != grids):
            raise ValueError("Require identical increasing decision grids")
        if [(s["decision_ts"], s["candle_id"]) for s in feature["states"]] != [
                (s["decision_ts"], s["candle_id"]) for s in joined["states"]]:
            raise ValueError("Feature/state alignment mismatch")
        grids = times
        sources[symbol], feature_hashes[symbol] = joined["join_sha256"], feature["features_sha256"]
        inputs[symbol] = (joined, feature, {a["candidate_id"]: a for a in joined["articles"]})
    if set(by_symbol) != set(sources):
        raise ValueError("Unexpected feature symbols")
    prepared = prepare_evidence(evidence, sources)
    latest, cursor, frames, counts = {}, 0, [], Counter()
    entry_counts, previous_rvol = Counter(), {}
    fallback_states = 0
    venue_states = 0
    partial_volume_states = 0
    for index, now in enumerate(grids):
        while cursor < len(prepared) and prepared[cursor]["available_ts"] <= now:
            row = prepared[cursor]
            key = (row["symbol"], row["kind"])
            old = latest.get(key)
            if old is None or (row["effective_ts"], row["available_ts"]) > (old["effective_ts"], old["available_ts"]):
                latest[key] = row
            cursor += 1
        candidates, rejected = [], {}
        for symbol, (joined, feature, articles) in sorted(inputs.items()):
            state, values = joined["states"][index], feature["states"][index]
            reasons = []
            price, shares = latest.get((symbol, "price")), latest.get((symbol, "float"))
            price_basis, price_value, price_id = "nominal", None, None
            price_known = (price is not None and now < price["valid_until_ts"]
                           and now - price["effective_ts"] <= config.snapshot_max_age_seconds)
            if price_known:
                price_value, price_id = price["value"], price["evidence_id"]
            elif (values.get("price_basis") == "nominal" and values["volume_source"] == "alpaca_sip"
                  and type(values.get("historical_nominal_price")) in (int, float)
                  and math.isfinite(values["historical_nominal_price"]) and values["historical_nominal_price"] > 0):
                price_value, price_id = values["historical_nominal_price"], state["candle_id"]
            elif allow_adjusted_price:
                value = values.get("observed_adjusted_close")
                if type(value) in (int, float) and math.isfinite(value) and value > 0:
                    price_value, price_basis = value, "adjusted_research_fallback"
                    fallback_states += 1
            if price_value is None:
                reasons.append("historical_nominal_price_unknown")
            elif not config.minimum_price <= price_value <= config.maximum_price:
                reasons.append("price_out_of_range")
            watch_reasons = reasons
            reasons = []
            if shares is None or now >= shares["valid_until_ts"]:
                reasons.append("historical_float_unknown")
            elif shares["basis"] == "sec_reported_nonaffiliate_proxy" and not allow_sec_float_proxy:
                reasons.append("sec_float_proxy_not_enabled")
            elif shares["value"] > config.maximum_float:
                reasons.append("float_above_threshold")
            rvol = values["rvol"]
            rvol_valid, rvol_basis = values["rvol_valid"], values.get("rvol_basis", "consolidated_bar_proxy")
            if (allow_partial_volume_rvol and values["volume_source"] in {"pitrading", "alpaca_sip"}
                    and not rvol_valid):
                rvol, rvol_valid = values.get("observed_volume_rvol"), values.get("observed_volume_rvol_valid", False)
                rvol_basis = "consolidated_partial_observation_proxy"
                if rvol_valid and type(rvol) in (int, float) and math.isfinite(rvol) and rvol >= 0:
                    partial_volume_states += 1
            venue_fallback = allow_venue_rvol and values["volume_source"] == "iex"
            if venue_fallback:
                rvol_basis = "iex_only_relative_activity"
                if not rvol_valid:
                    rvol = values.get("observed_volume_rvol")
                    rvol_valid = values.get("observed_volume_rvol_valid", False)
                    rvol_basis = "iex_only_partial_observation_proxy"
                if rvol_valid and type(rvol) in (int, float) and math.isfinite(rvol) and rvol >= 0:
                    venue_states += 1
            if values["volume_source"] not in {"pitrading", "alpaca_sip"} and not venue_fallback:
                reasons.append("consolidated_rvol_unavailable")
            elif not rvol_valid or type(rvol) not in (int, float) or not math.isfinite(rvol) or rvol < 0:
                reasons.append("rvol_unknown")
            elif rvol < config.minimum_rvol:
                reasons.append("rvol_below_threshold")
            if not values["regular_session"]:
                reasons.append("outside_regular_session")
            active = [i for i in state["news_candidate_ids"] if i in articles
                      and articles[i]["replay_available_ts"] <= now
                      < articles[i]["replay_available_ts"] + config.watch_window_seconds]
            valid_rvol = (rvol_valid and type(rvol) in (int, float) and math.isfinite(rvol) and rvol >= 0
                          and (values["volume_source"] in {"pitrading", "alpaca_sip"} or venue_fallback))
            triggered = valid_rvol and rvol >= config.minimum_rvol
            prior_rvol = previous_rvol.get(symbol)
            previous_rvol[symbol] = rvol if valid_rvol else None
            vwap = values.get("bar_vwap_estimate")
            if type(vwap) not in (int, float) or not math.isfinite(vwap) or vwap <= 0:
                reasons.append("vwap_unknown")
            elif price_value is not None and price_value <= vwap:
                reasons.append("price_not_above_vwap")
            macd, signal = values.get("minute_macd"), values.get("minute_macd_signal")
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (macd, signal)):
                reasons.append("macd_unknown")
            elif macd <= 0 or macd <= signal:
                reasons.append("macd_not_bullish")
            if not active:
                watch_reasons.append("news_watch_inactive")
            if watch_reasons:
                rejected[symbol] = watch_reasons
                continue
            float_known = shares is not None and now < shares["valid_until_ts"]
            candidates.append({"symbol": symbol, "price": price_value, "price_basis": price_basis, "rvol": rvol,
                "rvol_valid": valid_rvol, "momentum_triggered": triggered,
                "rvol_delta": rvol - prior_rvol if valid_rvol and prior_rvol is not None else None,
                "rvol_crossed_threshold": bool(triggered and prior_rvol is not None and prior_rvol < config.minimum_rvol),
                "stage": "entry_evaluation_ready" if not reasons else "evaluating" if triggered else "watching",
                "research_entry_ready": not reasons, "entry_block_reasons": reasons,
                "vwap": vwap, "macd": macd, "macd_signal": signal,
                "rvol_basis": rvol_basis, "volume_source": values["volume_source"],
                "prior_minute_coverage": values.get("prior_minute_coverage"),
                "current_minute_coverage": values.get("current_minute_coverage"),
                "float_shares": shares["value"] if float_known else None, "news_candidate_ids": sorted(active),
                "float_basis": shares["basis"] if float_known else None,
                "float_source": shares["source"] if float_known else None,
                "float_as_of_ts": shares["effective_ts"] if float_known else None,
                "float_available_ts": shares["available_ts"] if float_known else None,
                "float_age_seconds": now - shares["effective_ts"] if float_known else None,
                "evidence_ids": ([price_id] if price_id else []) + ([shares["evidence_id"]] if float_known else []),
                "entry_eligible": False, "research_watch_eligible": True})
        candidates.sort(key=lambda c: (not c["research_entry_ready"], not c["momentum_triggered"],
                                      -(c["rvol"] if c["rvol_valid"] else -1), c["symbol"]))
        for candidate in candidates[config.capacity:]:
            rejected[candidate["symbol"]] = ["watch_capacity"]
        selected = candidates[:config.capacity]
        entry_blocked = {c["symbol"]: c["entry_block_reasons"] for c in selected if c["entry_block_reasons"]}
        frames.append({"decision_ts": now, "watching": selected, "rejected": rejected,
                       "entry_blocked": entry_blocked})
        counts.update(reason for reasons in rejected.values() for reason in reasons)
        entry_counts.update(reason for reasons in entry_blocked.values() for reason in reasons)
    result = {"version": "historical-watch-v2", "config": asdict(config), "source_hashes": sources,
              "feature_hashes": feature_hashes, "evidence_sha256": hashlib.sha256(encode_dataset(evidence).encode()).hexdigest(),
              "evidence": prepared, "frames": frames, "rejection_counts": dict(counts),
              "entry_block_counts": dict(entry_counts),
              "watch_policy": "active_news_and_price_range_then_capacity",
              "entry_policy": "rvol_float_regular_session_above_vwap_positive_macd_above_signal_then_execution_risk",
              "research_policy": {"allow_adjusted_price": allow_adjusted_price, "allow_venue_rvol": allow_venue_rvol,
                                  "allow_partial_volume_rvol": allow_partial_volume_rvol,
                                  "allow_sec_float_proxy": allow_sec_float_proxy},
              "adjusted_price_fallback_states": fallback_states,
              "venue_rvol_states": venue_states,
              "partial_volume_rvol_states": partial_volume_states,
              "retrospective": True, "training_ready": False, "broker_calls": 0,
              "limitations": ["Caller-supplied evidence provenance is not independently authenticated",
                  "Strict PiTrading bar RVOL is a research proxy, not live Scanner certification",
                  "Retrospective news enrichment and assumed publication delay may introduce hindsight",
                  "Adjusted-price fallback may admit stocks outside historical nominal price targets",
                  "IEX-relative activity is not consolidated RVOL; unequal missingness and venue share can bias it",
                  "Observed-volume fallback retains gaps; unequal missingness may bias consolidated ratios too",
                  "SEC nonaffiliate shares are a dated proxy; expiry is not proof of complete corporate-action coverage",
                  "Watch admission is not trade permission; execution risk gates still apply"]}
    result["watch_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    import argparse
    import json
    from pathlib import Path
    from drl_agent.watch_replay import WatchConfig
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    manifest = json.loads(args.manifest.read_text())
    def read(path):
        return json.loads((args.manifest.parent / path).read_text())
    evidence = read(manifest["evidence"])
    if isinstance(evidence, dict):
        _verify(evidence, "sec-float-evidence-v1", "evidence_sha256")
        evidence = evidence["evidence"]
    result = build_watch([read(p) for p in manifest["joined"]], [read(p) for p in manifest["features"]],
                         evidence, config=WatchConfig(**manifest["config"]),
                         allow_adjusted_price=manifest.get("allow_adjusted_price", False),
                         allow_venue_rvol=manifest.get("allow_venue_rvol", False),
                         allow_partial_volume_rvol=manifest.get("allow_partial_volume_rvol", False),
                         allow_sec_float_proxy=manifest.get("allow_sec_float_proxy", False))
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({"watch_sha256": result["watch_sha256"], "frames": len(result["frames"]),
                         "watched_states": sum(bool(f["watching"]) for f in result["frames"]),
                         "entry_ready_candidates": sum(c["research_entry_ready"] for f in result["frames"]
                                                       for c in f["watching"]),
                         "entry_block_counts": result["entry_block_counts"],
                         "adjusted_price_fallback_states": result["adjusted_price_fallback_states"],
                         "venue_rvol_states": result["venue_rvol_states"],
                         "rejection_counts": result["rejection_counts"]}))


if __name__ == "__main__":
    main()
