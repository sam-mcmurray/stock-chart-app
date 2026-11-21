"""Cache SEC company facts and preserve dated shares/public-float disclosures."""
import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
import requests

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify

CONCEPTS = {
    "EntityCommonStockSharesOutstanding": ("shares", "shares_outstanding"),
    "EntityPublicFloat": ("USD", "public_float_value"),
}


def cache_companyfacts(cik, output, *, user_agent=None, snapshot=None, get=requests.get):
    if not re.fullmatch(r"[0-9]{1,10}", str(cik)) or int(cik) <= 0:
        raise ValueError("Invalid CIK")
    cik = str(int(cik)).zfill(10)
    snapshot = snapshot or datetime.now(timezone.utc).date().isoformat()
    if date.fromisoformat(snapshot).isoformat() != snapshot:
        raise ValueError("Invalid snapshot date")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"CIK{cik}_{snapshot}_companyfacts.json"
    lock = output / ("." + target.name + ".lock")
    with lock.open("x"):
        pass
    temporary = None
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        if target.exists():
            saved = json.loads(target.read_text())
            _verify(saved, "sec-companyfacts-cache-v1", "archive_sha256")
            if saved["url"] != url or int(saved["payload"]["cik"]) != int(cik):
                raise ValueError("Cache identity mismatch")
            return saved, True
        if snapshot != datetime.now(timezone.utc).date().isoformat():
            raise ValueError("Cannot download today's facts under an older snapshot date")
        if (not user_agent or not re.search(r"\S+@\S+\.\S+", user_agent)
                or "example.com" in user_agent.lower() or "\n" in user_agent or "\r" in user_agent):
            raise ValueError("Set SEC_USER_AGENT to the application name and a real contact email")
        with get(url, headers={"User-Agent": user_agent, "Accept": "application/json"},
                 timeout=(15, 60), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError(f"SEC companyfacts returned HTTP {response.status_code}; no automatic retries")
            chunks, size = [], 0
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > 64 * 1024 * 1024:
                    raise ValueError("Companyfacts response exceeds size budget")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
        if int(payload.get("cik", 0)) != int(cik) or not isinstance(payload.get("facts"), dict):
            raise ValueError("Unexpected SEC company identity or facts")
        result = {"version": "sec-companyfacts-cache-v1", "url": url,
                  "archived_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
        result["archive_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
        fd, name = tempfile.mkstemp(prefix=".sec_", suffix=".part", dir=output)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encode_dataset(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        return result, False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()


def build_history(archive):
    _verify(archive, "sec-companyfacts-cache-v1", "archive_sha256")
    payload = archive["payload"]
    cik = int(payload["cik"])
    concepts = payload.get("facts", {}).get("dei", {})
    rows, excluded, seen = [], Counter(), set()
    for concept, (unit, kind) in CONCEPTS.items():
        units = concepts.get(concept, {}).get("units", {})
        for other_unit, values in units.items():
            if other_unit != unit:
                excluded["unsupported_unit"] += len(values)
                continue
            for fact in values:
                try:
                    effective, filed = date.fromisoformat(fact["end"]), date.fromisoformat(fact["filed"])
                    value, accession = fact["val"], fact["accn"]
                    if (fact.get("start") or filed < effective or type(value) not in (int, float)
                            or not math.isfinite(value) or value < 0
                            or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)):
                        raise ValueError("Invalid instantaneous disclosure")
                except (KeyError, TypeError, ValueError):
                    excluded["invalid_fact"] += 1
                    continue
                identity = (concept, accession, effective.isoformat(), value)
                if identity in seen:
                    excluded["duplicate_fact"] += 1
                    continue
                seen.add(identity)
                # Companyfacts supplies a filing date, not an acceptance instant.
                # Withhold the fact for that entire New York date to avoid leakage.
                available = datetime.combine(filed + timedelta(days=1), time(), ZoneInfo("America/New_York"))
                row = {"cik": cik, "kind": kind, "concept": "dei:" + concept, "value": value, "unit": unit,
                       "effective_date": effective.isoformat(), "filed_date": filed.isoformat(),
                       "available_ts": int(available.timestamp()), "form": fact.get("form"),
                       "accession": accession,
                       "filing_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.html",
                       "availability_basis": "day_after_filed_date_midnight_America_New_York",
                       "float_share_count": None, "reported_not_estimated": True}
                row["record_id"] = hashlib.sha256(encode_dataset(row).encode()).hexdigest()
                rows.append(row)
    rows.sort(key=lambda r: (r["available_ts"], r["effective_date"], r["record_id"]))
    result = {"version": "sec-share-history-v1", "cik": cik, "entity_name": payload.get("entityName"),
              "source_archive_sha256": archive["archive_sha256"], "records": rows, "excluded": dict(excluded),
              "counts": dict(Counter(r["kind"] for r in rows)), "training_ready": False,
              "limitations": ["Shares outstanding are not tradable float",
                  "EntityPublicFloat is USD, not a float-share count",
                  "Companyfacts omits custom and some class-specific facts; no universal historical coverage",
                  "Later filings/revisions retain their own availability; no backward filling",
                  "Count changes are not automatically classified as completed dilution",
                  "Float-share estimates require matched reference prices and equity-class scope"]}
    result["history_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result


def facts_as_of(history, timestamp):
    """Return last-known facts, retaining conflicting values rather than choosing one."""
    _verify(history, "sec-share-history-v1", "history_sha256")
    if type(timestamp) is not int or timestamp <= 0:
        raise ValueError("Invalid decision timestamp")
    result = {}
    for kind in ("shares_outstanding", "public_float_value"):
        rows = [r for r in history["records"] if r["kind"] == kind and r["available_ts"] <= timestamp]
        if not rows:
            continue
        latest = max((r["effective_date"], r["available_ts"]) for r in rows)
        chosen = [dict(r) for r in rows if (r["effective_date"], r["available_ts"]) == latest]
        result[kind] = {"records": chosen, "conflicting": len({r["value"] for r in chosen}) > 1}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cik", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    archive, reused = cache_companyfacts(args.cik, args.output,
        user_agent=os.getenv("SEC_USER_AGENT") or env.get("SEC_USER_AGENT"))
    history = build_history(archive)
    target = args.output / f"CIK{int(args.cik):010d}_{archive['archive_sha256'][:16]}_history.json"
    if target.exists():
        if json.loads(target.read_text()) != history:
            raise ValueError("Existing history differs; not overwritten")
    else:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(encode_dataset(history) + "\n")
    print(encode_dataset({"file": str(target), "reused": reused, "counts": history["counts"],
                         "excluded": history["excluded"], "history_sha256": history["history_sha256"]}))


if __name__ == "__main__":
    main()
