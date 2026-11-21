"""Read-only regular-session price coverage for retrospective news manifests."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import exchange_calendars as xcals
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from drl_agent.dataset import encode_dataset


def request_intervals(manifest):
    unsigned = deepcopy(manifest)
    digest = unsigned.pop("manifest_sha256", None)
    if digest != hashlib.sha256(encode_dataset(unsigned).encode()).hexdigest():
        raise ValueError("Manifest digest mismatch")
    if manifest.get("version") != "retrospective-news-universe-v1":
        raise ValueError("Unsupported manifest")
    requests = manifest["price_requests"]
    if len(requests) > 100000:
        raise ValueError("Too many price requests")
    if not requests:
        return []
    seen = set()
    for row in requests:
        if (type(row["start_ts"]) is not int or type(row["end_ts"]) is not int
                or not 0 < row["start_ts"] < row["end_ts"] <= row["start_ts"] + 86400
                or row.get("frequency_minutes") != 1 or not row.get("symbol")
                or not row.get("request_id") or row["request_id"] in seen):
            raise ValueError("Invalid or duplicate price request")
        seen.add(row["request_id"])
    first = datetime.fromtimestamp(min(r["start_ts"] for r in requests), timezone.utc).date()
    last = datetime.fromtimestamp(max(r["end_ts"] for r in requests), timezone.utc).date()
    calendar = xcals.get_calendar("XNYS", start=first - timedelta(days=7), end=last + timedelta(days=7))
    sessions = [(int(calendar.session_open(s).timestamp()), int(calendar.session_close(s).timestamp()))
                for s in calendar.sessions_in_range(str(first), str(last))]
    intervals = []
    for request in requests:
        for opening, closing in sessions:
            # Requests own candle start timestamps in [start, end). Session close
            # is exclusive; early closes and holidays come from the calendar.
            start = max(opening, ((request["start_ts"] + 59) // 60) * 60)
            end = min(closing, ((request["end_ts"] + 59) // 60) * 60)
            if start < end:
                intervals.append({"request_id": request["request_id"], "symbol": request["symbol"],
                                  "start_ts": start, "end_ts": end})
    return intervals


def load_counts(intervals):
    if not intervals:
        return []
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '30s'")
        return conn.execute(
            """WITH windows AS (
                 SELECT * FROM jsonb_to_recordset(%(windows)s::jsonb)
                   AS w(request_id text, symbol text, start_ts bigint, end_ts bigint)
               )
               SELECT w.request_id, SUM(c.minutes)::bigint AS present_minutes
               FROM windows w CROSS JOIN LATERAL (
                 SELECT COUNT(DISTINCT m.event_ts) AS minutes FROM market_observations m
                 WHERE m.symbol = w.symbol AND m.event_ts >= w.start_ts AND m.event_ts < w.end_ts
                   AND m.event_ts %% 60 = 0
                   AND m.observation_type = 'historical_minute_candle'
                   AND m.source = 'schwab_price_history'
                   AND m.payload->>'frequency_minutes' = '1'
                   AND m.open > 0 AND m.high > 0 AND m.low > 0 AND m.close > 0
                   AND m.open < 'Infinity'::float8 AND m.high < 'Infinity'::float8
                   AND m.low < 'Infinity'::float8 AND m.close < 'Infinity'::float8
                   AND m.volume >= 0 AND m.volume < 'Infinity'::float8
                   AND m.high >= GREATEST(m.open, m.close, m.low)
                   AND m.low <= LEAST(m.open, m.close)
               ) c GROUP BY w.request_id""", {"windows": Jsonb(intervals)}).fetchall()


def build_report(manifest, intervals, counts):
    expected = Counter()
    for interval in intervals:
        expected[interval["request_id"]] += (interval["end_ts"] - interval["start_ts"]) // 60
    present = {}
    for row in counts:
        key, value = row["request_id"], row["present_minutes"]
        if key in present or key not in expected or type(value) is not int or not 0 <= value <= expected[key]:
            raise ValueError("Invalid coverage counts")
        present[key] = value
    output = []
    for request in manifest["price_requests"]:
        key = request["request_id"]
        needed, found = expected[key], present.get(key, 0)
        status = ("no_regular_session" if not needed else "regular_minutes_present" if found == needed
                  else "partial_regular_minutes" if found else "missing_regular_minutes")
        output.append({**request, "status": status, "expected_regular_minutes": needed,
                       "present_regular_minutes": found, "missing_regular_minutes": needed - found})
    result = {"version": "historical-price-coverage-v1", "source_manifest_sha256": manifest["manifest_sha256"],
              "calendar": "XNYS", "scope": "regular_session_only",
              "observation_source": "schwab_price_history", "frequency_minutes": 1,
              "requests": output, "status_counts": dict(Counter(r["status"] for r in output)),
              "expected_regular_minutes": sum(expected.values()), "present_regular_minutes": sum(present.values()),
              "training_ready": False, "broker_calls": 0,
              "limitations": ["Missing bars may be halts or no trades, not provider omissions",
                              "Extended hours, corporate actions and symbol history are not certified",
                              "Import task completion is not candle coverage"]}
    result["report_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    manifest = json.loads(args.manifest.read_text())
    intervals = request_intervals(manifest)
    result = build_report(manifest, intervals, load_counts(intervals))
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({k: v for k, v in result.items() if k != "requests"}))


if __name__ == "__main__":
    main()
