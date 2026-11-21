"""Detect disclosed outstanding-share changes without claiming their cause."""
import argparse
from datetime import date, datetime, time
import hashlib
from itertools import groupby
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def build_changes(history, symbol):
    _verify(history, "sec-share-history-v1", "history_sha256")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol):
        raise ValueError("Invalid symbol")
    rows = [r for r in history["records"] if r["kind"] == "shares_outstanding"]
    for row in rows:
        if (row["unit"] != "shares" or type(row["value"]) not in (int, float)
                or not math.isfinite(row["value"]) or row["value"] <= 0):
            raise ValueError("Invalid outstanding-share disclosure")
    rows.sort(key=lambda r: (r["available_ts"], r["effective_date"], r["record_id"]))
    baseline, latest_date, events, excluded = None, None, [], []
    for available, batch in groupby(rows, key=lambda r: r["available_ts"]):
        batch = list(batch)
        measured = max(r["effective_date"] for r in batch)
        if latest_date is not None and measured < latest_date:
            excluded.append({"available_ts": available, "reason": "older_measurement_arrived_later"})
            continue
        chosen = [r for r in batch if r["effective_date"] == measured]
        if len({r["value"] for r in chosen}) != 1:
            excluded.append({"available_ts": available, "reason": "conflicting_latest_counts",
                             "record_ids": [r["record_id"] for r in chosen]})
            effective = int(datetime.combine(date.fromisoformat(measured), time(),
                                             ZoneInfo("America/New_York")).timestamp())
            events.append({"symbol": symbol, "cik": history["cik"], "kind": "other_supply_change",
                "effective_ts": effective, "available_ts": available,
                "availability_basis": chosen[0]["availability_basis"],
                "change_basis": "conflicting_disclosures_uncertainty", "cause": "unknown",
                "confirmed_dilution": False, "source": chosen[0]["filing_url"],
                "source_record_ids": [r["record_id"] for r in chosen],
                "source_history_sha256": history["history_sha256"],
                "symbol_mapping_basis": "caller_supplied_cik_mapping"})
            baseline, latest_date = None, measured
            continue
        current = chosen[0]
        if baseline is not None and current["value"] != baseline["value"]:
            effective = int(datetime.combine(date.fromisoformat(measured), time(),
                                             ZoneInfo("America/New_York")).timestamp())
            if not 0 < effective <= available:
                raise ValueError("Invalid disclosure timing")
            delta = current["value"] - baseline["value"]
            # Even a revision makes the earlier evidence suspect, but it is not
            # proof that an issuance or another economic event actually occurred.
            events.append({"symbol": symbol, "cik": history["cik"], "kind": "other_supply_change",
                "effective_ts": effective, "available_ts": available,
                "availability_basis": current["availability_basis"],
                "change_basis": "same_date_disclosure_revision" if measured == baseline["effective_date"]
                                else "different_dated_outstanding_counts",
                "direction": "increase" if delta > 0 else "decrease", "cause": "unknown",
                "confirmed_dilution": False, "old_count": baseline["value"], "new_count": current["value"],
                "change_shares": delta, "change_fraction": delta / baseline["value"],
                "interval_start_date": baseline["effective_date"], "interval_end_date": measured,
                "effective_date_basis": "measurement_interval_endpoint_not_actual_transaction_date",
                "source": current["filing_url"], "prior_source": baseline["filing_url"],
                "source_record_id": current["record_id"], "prior_record_id": baseline["record_id"],
                "source_history_sha256": history["history_sha256"],
                "symbol_mapping_basis": "caller_supplied_cik_mapping"})
        baseline, latest_date = current, measured
    result = {"version": "corporate-action-invalidations-v1", "invalidations": events,
              "excluded": excluded, "source_history_sha256": history["history_sha256"],
              "coverage_complete": False, "symbol": symbol, "cik": history["cik"],
              "limitations": ["Count differences do not prove dilution, an offering, a split or tradable-float change",
                  "Unknown transaction date; invalidation occurs only when the new filing becomes available",
                  "Caller-supplied symbol mapping is not historical ticker/class certification",
                  "Conflicting counts reset the comparison baseline; no complete event coverage claim"]}
    result["invalidations_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_changes(json.loads(args.history.read_text()), args.symbol)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({"events": len(result["invalidations"]), "excluded": result["excluded"],
                         "invalidations_sha256": result["invalidations_sha256"]}))


if __name__ == "__main__":
    main()
