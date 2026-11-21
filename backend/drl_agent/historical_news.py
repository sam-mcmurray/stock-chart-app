"""Retrospective news universe and bounded price requests; no broker or DB writes."""
import argparse
from collections import Counter
import hashlib
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from drl_agent.dataset import encode_dataset

VERSION = "retrospective-news-universe-v1"


def validate_window(start_ts, end_ts, enrichment_cutoff_ts):
    if any(type(v) is not int or v <= 0 for v in (start_ts, end_ts, enrichment_cutoff_ts)):
        raise ValueError("Timestamps must be positive integers")
    if not start_ts < end_ts <= enrichment_cutoff_ts or end_ts - start_ts > 31 * 86400:
        raise ValueError("Provide a half-open window of at most 31 days before the enrichment cutoff")


def load_versions(*, start_ts, end_ts, enrichment_cutoff_ts, limit=100000):
    validate_window(start_ts, end_ts, enrichment_cutoff_ts)
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError("limit must be 1..100000")
    versions = {}
    # Bound cold database reads without changing the frozen latest-version lookup.
    for first in range(start_ts, end_ts, 3600):
        rows = _load_bounded_window(start_ts=first, end_ts=min(first + 3600, end_ts),
                                   enrichment_cutoff_ts=enrichment_cutoff_ts, limit=limit)
        for row in rows:
            key = (row['record_type'], row['record_id'])
            old = versions.get(key)
            if old is None or (row['observed_ts'], row['version_key']) > (old['observed_ts'], old['version_key']):
                versions[key] = row
        if len(versions) > limit:
            raise ValueError("Article limit exceeded; shorten the publication window")
    return [versions[key] for key in sorted(versions)]


def _load_bounded_window(*, start_ts, end_ts, enrichment_cutoff_ts, limit):
    try:
        return _load_version_window(start_ts=start_ts, end_ts=end_ts,
                                    enrichment_cutoff_ts=enrichment_cutoff_ts, limit=limit)
    except psycopg.errors.QueryCanceled:
        if end_ts - start_ts <= 60:
            raise
        # The failed transaction has closed. Split only that window, with a
        # finite one-minute floor; never swallow errors or return a partial day.
        middle = (start_ts + end_ts) // 2
        return (_load_bounded_window(start_ts=start_ts, end_ts=middle,
                    enrichment_cutoff_ts=enrichment_cutoff_ts, limit=limit)
                + _load_bounded_window(start_ts=middle, end_ts=end_ts,
                    enrichment_cutoff_ts=enrichment_cutoff_ts, limit=limit))


def _load_version_window(*, start_ts, end_ts, enrichment_cutoff_ts, limit):
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '30s'")
        # Resolve latest frozen versions before checking eligibility. A later
        # correction must not resurrect an older, incorrectly linked version.
        rows = conn.execute(
            """WITH identities AS MATERIALIZED (
                 SELECT DISTINCT record_type, record_id FROM news_enrichment_versions
                 WHERE published_ts >= %(start)s AND published_ts < %(end)s
                   AND observed_ts <= %(cutoff)s
                 ORDER BY record_type, record_id LIMIT %(limit)s
               )
               SELECT v.* FROM identities i CROSS JOIN LATERAL (
                 SELECT v.version_key, v.record_type, v.record_id, v.article_url,
                        v.published_ts, v.observed_ts, v.pipeline_version, v.result
                 FROM news_enrichment_versions v
                 WHERE v.record_type = i.record_type AND v.record_id = i.record_id
                   AND v.observed_ts <= %(cutoff)s
                 ORDER BY v.observed_ts DESC, v.version_key DESC LIMIT 1
               ) v ORDER BY i.record_type, i.record_id""",
            {"start": start_ts, "end": end_ts, "cutoff": enrichment_cutoff_ts,
             "limit": limit + 1}).fetchall()
    if len(rows) > limit:
        raise ValueError("Article limit exceeded; shorten the publication window")
    return rows


def build_universe(rows, *, start_ts, end_ts, enrichment_cutoff_ts,
                   processing_delay_seconds=60, watch_days=7, price_lookback_days=30):
    validate_window(start_ts, end_ts, enrichment_cutoff_ts)
    for value, minimum, maximum in ((processing_delay_seconds, 0, 86400),
                                    (watch_days, 1, 30), (price_lookback_days, 1, 365)):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("Invalid delay or price-window bounds")
    if len(rows) > 100000:
        raise ValueError("Article limit exceeded")
    versions, rejected = {}, Counter()
    for row in rows:
        observed = row.get("observed_ts")
        if type(observed) is not int or observed <= 0:
            raise ValueError("Invalid enrichment timestamp")
        if observed > enrichment_cutoff_ts:
            rejected["after_enrichment_cutoff"] += 1
            continue
        if any(not isinstance(row.get(k), str) or not row[k].strip()
               for k in ("record_type", "record_id", "version_key")):
            raise ValueError("Missing article identity")
        key = (row["record_type"], row["record_id"])
        old = versions.get(key)
        if old is None or (observed, row["version_key"]) > (old["observed_ts"], old["version_key"]):
            versions[key] = row
    candidates, intervals = [], {}
    for key, row in sorted(versions.items()):
        published = row.get("published_ts")
        if type(published) is not int or not start_ts <= published < end_ts:
            rejected["publication_outside_window"] += 1
            continue
        result = row.get("result") or {}
        if result.get("relevance_label") not in {"finance", "business", "markets"}:
            rejected["unrelated"] += 1
            continue
        if result.get("article_verification_status") not in {"canonical", "publisher", "mention", "gdelt_mention"}:
            rejected["unverified_article"] += 1
            continue
        link_version = result.get("entity_link_version")
        if type(link_version) is not int or link_version < 2:
            rejected["unverified_company_links"] += 1
            continue
        raw_tickers = result.get("related_tickers")
        if not isinstance(raw_tickers, list) or any(not isinstance(s, str) for s in raw_tickers):
            rejected["invalid_tickers"] += 1
            continue
        tickers = sorted({s.strip().upper() for s in raw_tickers if s.strip()})
        if not tickers:
            rejected["no_company_tickers"] += 1
            continue
        available = published + processing_delay_seconds
        if available >= enrichment_cutoff_ts:
            rejected["replay_availability_after_cutoff"] += 1
            continue
        identity = hashlib.sha256(encode_dataset([VERSION, *key, row["version_key"],
                                                 processing_delay_seconds]).encode()).hexdigest()
        candidates.append({"candidate_id": identity, "record_type": key[0], "record_id": key[1],
                           "enrichment_version_key": row["version_key"],
                           "pipeline_version": row.get("pipeline_version"),
                           "article_url": row.get("article_url"), "tickers": tickers,
                           "published_ts": published, "enriched_observed_ts": row["observed_ts"],
                           "replay_available_ts": available,
                           "verification": result["article_verification_status"],
                           "retrospective": True, "live_activation_eligible": False})
        for symbol in tickers:
            intervals.setdefault(symbol, []).append((max(1, available - price_lookback_days * 86400),
                                                     min(enrichment_cutoff_ts, available + watch_days * 86400)))
    requests = []
    for symbol, windows in sorted(intervals.items()):
        merged = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in merged:
            while start < end:
                stop = min(end, start + 86400)
                key = hashlib.sha256(encode_dataset([symbol, start, stop, 1]).encode()).hexdigest()
                requests.append({"request_id": key, "symbol": symbol, "start_ts": start,
                                 "end_ts": stop, "frequency_minutes": 1,
                                 "status": "coverage_required", "provider": None})
                if len(requests) > 100000:
                    raise ValueError("Price-request limit exceeded; shorten publication window")
                start = stop
    report = {"version": VERSION, "publication_window": {"start_ts": start_ts, "end_ts": end_ts},
              "enrichment_cutoff_ts": enrichment_cutoff_ts,
              "processing_delay_seconds": processing_delay_seconds,
              "watch_days": watch_days, "price_lookback_days": price_lookback_days,
              "retrospective": True, "availability_basis": "publication_plus_assumed_delay",
              "hindsight_risk": "Modern extraction and revised publisher content may contain later information",
              "universe_basis": "verified_company_linked_news_not_future_returns_or_current_scanner",
              "training_ready": False, "live_activation_eligible": False, "broker_calls": 0,
              "input_version_count": len(rows), "selected_article_count": len(candidates),
              "rejections": dict(sorted(rejected.items())),
              "candidates": sorted(candidates, key=lambda c: (c["replay_available_ts"], c["candidate_id"])),
              "price_requests": requests}
    report["manifest_sha256"] = hashlib.sha256(encode_dataset(report).encode()).hexdigest()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("start-ts", "end-ts", "enrichment-cutoff-ts"):
        parser.add_argument("--" + name, type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    options = {k: getattr(args, k) for k in ("start_ts", "end_ts", "enrichment_cutoff_ts")}
    report = build_universe(load_versions(**options), **options)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(report) + "\n")
    print(encode_dataset({k: v for k, v in report.items() if k not in {"candidates", "price_requests"}}))


if __name__ == "__main__":
    main()
