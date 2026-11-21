"""Convert verified SEC count artifacts into explicitly dated research evidence."""
import argparse
from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def build_evidence(disclosures, *, maximum_age_days, invalidations=()):
    if type(maximum_age_days) is not int or not 1 <= maximum_age_days <= 730:
        raise ValueError("maximum_age_days must be an integer from 1 to 730")
    invalidations_hash = None
    if isinstance(invalidations, dict):
        _verify(invalidations, "corporate-action-invalidations-v1", "invalidations_sha256")
        invalidations_hash = invalidations["invalidations_sha256"]
        invalidations = invalidations["invalidations"]
    events = []
    for event in invalidations:
        if (not isinstance(event.get("symbol"), str) or not event["symbol"]
                or event.get("kind") not in {"split", "dilution", "share_class_change", "other_supply_change"}
                or any(type(event.get(k)) is not int or event[k] <= 0 for k in ("effective_ts", "available_ts"))
                or not isinstance(event.get("source"), str) or not event["source"].strip()):
            raise ValueError("Invalid corporate-action evidence")
        events.append(dict(event))
    rows, excluded, sources, identities = [], [], [], set()
    for artifact in disclosures:
        _verify(artifact, "sec-filing-float-v1", "float_sha256")
        if artifact["float_sha256"] in sources:
            continue
        sources.append(artifact["float_sha256"])
        for record in artifact["records"]:
            if (record.get("basis") != "reported_nonaffiliate_shares"
                    or record.get("reported_not_estimated") is not True or record.get("unit") != "shares"
                    or type(record.get("value")) is not int or record["value"] <= 0
                    or type(record.get("available_ts")) is not int
                    or not isinstance(record.get("symbol"), str) or not record["symbol"]):
                raise ValueError("Invalid reported nonaffiliate record")
            effective = int(datetime.combine(date.fromisoformat(record["effective_date"]), time(),
                                             ZoneInfo("America/New_York")).timestamp())
            available = record["available_ts"]
            if not 0 < effective <= available:
                raise ValueError("Invalid disclosure availability")
            expiry = effective + maximum_age_days * 86400
            # Announcements invalidate at effectiveness; late-discovered events
            # invalidate at availability, never before they could be known.
            relevant = [e for e in events if e["symbol"] == record["symbol"]
                        and (e.get("cik") is None or e["cik"] == record["cik"])
                        and e["effective_ts"] >= effective]
            if relevant:
                expiry = min(expiry, min(max(e["effective_ts"], e["available_ts"]) for e in relevant))
            if expiry <= available:
                excluded.append({"record_id": record["record_id"], "reason": "expired_before_available"})
                continue
            identity = (record["symbol"], effective, available)
            if identity in identities:
                raise ValueError("Conflicting or duplicate SEC disclosure identity")
            identities.add(identity)
            rows.append({"symbol": record["symbol"], "kind": "float", "value": record["value"],
                         "basis": "sec_reported_nonaffiliate_proxy", "source": record["source_url"],
                         "effective_ts": effective, "available_ts": available, "valid_until_ts": expiry,
                         "source_record_id": record["record_id"], "source_float_sha256": artifact["float_sha256"],
                         "filing_archive_sha256": record["filing_archive_sha256"],
                         "equity_class": record["equity_class"], "cik": record["cik"],
                         "maximum_age_days": maximum_age_days,
                         "corporate_action_coverage": "caller_supplied_not_certified_complete"})
    rows.sort(key=lambda r: (r["symbol"], r["effective_ts"], r["available_ts"]))
    result = {"version": "sec-float-evidence-v1", "evidence": rows, "excluded": excluded,
              "source_float_hashes": sorted(sources), "invalidations": events,
              "source_invalidations_sha256": invalidations_hash,
              "maximum_age_days": maximum_age_days, "training_ready": False,
              "limitations": ["Nonaffiliate count is not current tradable supply",
                  "Caller must supply corporate actions; missing events are not discovered by this exporter",
                  "No split adjustment, dilution estimate or symbol remapping is invented"]}
    result["evidence_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disclosure", type=Path, action="append", required=True)
    parser.add_argument("--maximum-age-days", type=int, required=True)
    parser.add_argument("--invalidations", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_evidence([json.loads(p.read_text()) for p in args.disclosure],
        maximum_age_days=args.maximum_age_days,
        invalidations=json.loads(args.invalidations.read_text()) if args.invalidations else [])
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({"records": len(result["evidence"]), "excluded": len(result["excluded"]),
                         "evidence_sha256": result["evidence_sha256"]}))


if __name__ == "__main__":
    main()
