"""Read-only, bounded publisher-content audit, not a semantic accuracy benchmark."""

import argparse
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from workers.news_llm_evaluator import evaluate


HEARTBEATS = ("calendar:relevance:heartbeat", "news:historical:llm:heartbeat",
              "news:historical:llm:helper:heartbeat")


def snapshots(redis):
    return {key: json.loads(redis.get(key) or "null") for key in HEARTBEATS}


def select_candidates(recent, historical):
    selected, urls, domains = [], set(), set()
    counts = {"recent": 0, "historical_older": 0, "historical_newer": 0}
    for group, rows, limit in (
        ("recent", recent, 8),
        ("historical_older", [r for r in historical if 0 < int(r.get("published_ts") or 0) < 1577836800], 4),
        ("historical_newer", [r for r in historical if int(r.get("published_ts") or 0) >= 1577836800], 4),
    ):
        for row in rows:
            url = str(row.get("url") or "")
            try:
                parsed = urlsplit(url)
                domain = (parsed.hostname or "").lower().removeprefix("www.")
            except ValueError:
                continue
            if parsed.scheme not in {"http", "https"} or not domain or url in urls or domain in domains:
                continue
            if counts[group] == limit:
                break
            selected.append({**row, "group": group})
            urls.add(url)
            domains.add(domain)
            counts[group] += 1
    return selected


def load_candidates(redis):
    import psycopg
    from psycopg.rows import dict_row

    ids = redis.zrevrange("news:recent:articles", 0, 127)
    recent = []
    for aid in ids:
        value = redis.hget("news:article:" + aid, "json")
        if value:
            recent.append(json.loads(value))
    # Do not call ensure_schema: evaluation must never run production migrations.
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '15s'")
        conn.execute("SET LOCAL lock_timeout = '2s'")
        historical = conn.execute("""
            SELECT url, original_title AS title, published_ts, source
            FROM historical_article_enrichments
            WHERE NOT (result ? 'processing_error')
            ORDER BY processed_at DESC, published_ts DESC LIMIT 256
        """).fetchall()
    return select_candidates(recent, historical)


def normalized(value):
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def consistency_checks(case, result):
    if not result:
        return ["missing_result"]
    article = case["article"]
    source = normalized(" ".join(str(article.get(k) or "") for k in ("title", "summary", "article_text")))
    issues = []
    for key in ("display_headline", "llm_summary"):
        value = str(result.get(key) or "")
        if not value.strip():
            issues.append("empty_" + key)
        elif html.unescape(value) != value:
            issues.append("html_entities_" + key)
    grounded = set(article.get("publisher_tickers") or [])
    for company in result.get("related_companies") or []:
        evidence = normalized(company.get("evidence"))
        if not evidence or evidence not in source:
            issues.append("company_evidence_not_in_input")
        grounded.update(company.get("validated_tickers") or [])
    for relation in result.get("company_relationships") or []:
        evidence = normalized(relation.get("evidence"))
        if not evidence or evidence not in source:
            issues.append("relationship_evidence_not_in_input")
        grounded.update(relation.get("validated_tickers_a") or [])
        grounded.update(relation.get("validated_tickers_b") or [])
    if set(result.get("related_tickers") or []) - grounded:
        issues.append("tickers_without_provenance")
    return sorted(set(issues))


def collect_inputs(candidates, fetch):
    from services.news_article_resolver import _headlines_match

    samples, skipped = [], []
    for candidate in candidates:
        metadata = fetch(candidate["url"])
        title = str(metadata.get("title") or "").strip()
        body = str(metadata.get("article_text") or "").strip()
        summary = str(metadata.get("summary") or "").strip()
        if not title or not (body or summary):
            skipped.append({**candidate, "reason": "publisher_content_unavailable"})
            continue
        reference = str(candidate.get("title") or "")
        if not reference or (normalized(reference) != normalized(title) and not _headlines_match(reference, title)):
            skipped.append({**candidate, "reason": "publisher_identity_not_confirmed", "fetched_title": title})
            continue
        # Preserve exactly the bounded publisher input used by the classifier;
        # never substitute an existing LLM summary for source evidence.
        article = {"id": hashlib.sha256(candidate["url"].encode()).hexdigest(),
                   "url": candidate["url"], "resolved_url": candidate["url"],
                   "source": candidate.get("source"), "title": title,
                   "original_title": title, "summary": summary[:1000],
                   "article_text": body[:2500], "publisher_tickers": metadata.get("tickers") or [],
                   "published_ts": candidate.get("published_ts"),
                   "content_verified": True, "headline_resolution_source": "canonical",
                   "content_resolution_source": "canonical" if body else "canonical_metadata_only"}
        samples.append({"article": article, "group": candidate["group"], "expected": None})
    return samples, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--inputs", help="Replay a saved input snapshot without fetching publisher pages or querying article storage")
    args = parser.parse_args()
    path = Path(args.output)
    with path.open("x", encoding="utf-8") as output:
        import redis
        from services import calendar_relevance
        from services.article_metadata import fetch_article_metadata

        client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        started = time.time()
        before = snapshots(client)
        if args.inputs:
            snapshot = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
            samples, skipped = snapshot["samples"], snapshot.get("skipped", [])
            if not isinstance(samples, list) or not 1 <= len(samples) <= 16:
                raise ValueError("Replay requires between 1 and 16 saved inputs")
            selected_count = len(samples) + len(skipped)
        else:
            candidates = load_candidates(client)
            print(f"Selected {len(candidates)} publisher URLs", flush=True)
            samples, skipped = collect_inputs(candidates, fetch_article_metadata)
            selected_count = len(candidates)
            snapshot = {"samples": samples, "skipped": skipped, "captured_at": int(time.time())}
        # Retain inputs even if inference is interrupted.
        with path.with_suffix(".inputs.json").open("x", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        print(f"Retrieved {len(samples)} inputs; unavailable {len(skipped)}", flush=True)
        report = evaluate(calendar_relevance, samples, consistency_checks)
        report["consistency_checks_passed"] = report.pop("passed")
        report.update({"semantic_accuracy": None, "selected": selected_count, "skipped": skipped,
                       "replay_input_path": args.inputs,
                       "started_at": started, "elapsed_seconds": round(time.time() - started, 3),
                       "heartbeats_before": before, "heartbeats_after": snapshots(client),
                       "limitations": "Convenience sample from latest retained feed and recently processed historical URLs, one per host. Publisher pages fetched now; no mentions fallback. Previous AI labels/summaries are not ground truth. Checks measure output consistency, not semantic correctness, missed companies, or population accuracy. Timing includes production contention; no causal slowdown estimate."})
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({k: report[k] for k in ("selected", "total", "consistency_checks_passed", "requests", "verification_requests", "request_wall_seconds", "verification_wall_seconds", "elapsed_seconds")}))
    for row in report["rows"]:
        print(row["group"], row["input"]["url"], row["errors"], flush=True)
    raise SystemExit(0 if report["total"] and report["consistency_checks_passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
