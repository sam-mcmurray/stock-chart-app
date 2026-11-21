"""Cache corporate actions locally; export explicitly timed split invalidations."""
import argparse
from datetime import date, datetime, time, timezone
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

URL = "https://data.alpaca.markets/v1/corporate-actions"


def _hash(data):
    return hashlib.sha256(encode_dataset(data).encode()).hexdigest()


def cache_actions(symbol, start, end, output, *, key=None, secret=None, get=requests.get):
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol):
        raise ValueError("Invalid symbol")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or not 0 <= (last - first).days <= 366:
        raise ValueError("Require ISO date window of at most 367 inclusive days")
    params = dict(symbols=symbol, start=start, end=end, limit=1000, sort="asc", region="us", data_quality="all")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{symbol}_{start}_{end}_actions.json"
    lock = output / ("." + target.name + ".lock")
    with lock.open("x"):
        pass
    temporary = None
    try:
        if target.exists():
            saved = json.loads(target.read_text())
            _verify(saved, "alpaca-actions-cache-v1", "archive_sha256")
            if saved["url"] != URL or saved["request"] != params:
                raise ValueError("Action cache identity mismatch")
            return saved, True
        if not key or not secret:
            raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY")
        pages, tokens, token, count, total_bytes = [], set(), None, 0, 0
        for _ in range(100):
            query = {**params, **({"page_token": token} if token else {})}
            try:
                with get(URL, params=query, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                         timeout=(15, 60), allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"Alpaca corporate actions returned HTTP {response.status_code}; no retries")
                    chunks, size = [], 0
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        total_bytes += len(chunk)
                        if size > 16 * 1024 * 1024 or total_bytes > 64 * 1024 * 1024:
                            raise ValueError("Action response exceeds size budget")
                        chunks.append(chunk)
                    payload = json.loads(b"".join(chunks))
            except requests.RequestException:
                raise RuntimeError("Alpaca corporate-action connection failed; credentials not logged") from None
            groups = payload.get("corporate_actions")
            if not isinstance(groups, dict) or any(not isinstance(rows, list) or
                    any(not isinstance(r, dict) for r in rows) for rows in groups.values()):
                raise ValueError("Unexpected corporate-action response")
            count += sum(len(rows) for rows in groups.values())
            if count > 100000:
                raise ValueError("Action row budget exceeded")
            pages.append(payload)
            token = payload.get("next_page_token")
            if not token:
                break
            if not isinstance(token, str) or token in tokens:
                raise ValueError("Invalid/repeated action pagination token")
            tokens.add(token)
        else:
            raise ValueError("Action pagination budget exceeded")
        saved = {"version": "alpaca-actions-cache-v1", "url": URL, "request": params,
                 "archived_at": datetime.now(timezone.utc).isoformat(), "responses": pages,
                 "rows": count, "coverage_complete": False,
                 "limitations": ["Query dates filter process dates, not all effective dates",
                     "Provider does not guarantee publication timing or complete dilution coverage",
                     "Immutable snapshot; later vendor corrections require a separate archive directory"]}
        saved["archive_sha256"] = _hash(saved)
        fd, name = tempfile.mkstemp(prefix=".actions_", suffix=".part", dir=output)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encode_dataset(saved) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        return saved, False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()


def split_invalidations(archive, *, allow_effective_date_proxy=False):
    _verify(archive, "alpaca-actions-cache-v1", "archive_sha256")
    if type(allow_effective_date_proxy) is not bool:
        raise ValueError("Research option must be boolean")
    if archive["url"] != URL:
        raise ValueError("Unexpected actions endpoint")
    collected = datetime.fromisoformat(archive["archived_at"])
    if collected.tzinfo is None:
        raise ValueError("Collection timezone required")
    observed = math.ceil(collected.timestamp())
    events, excluded, seen = [], [], {}
    for page in archive["responses"]:
        for kind, rows in page["corporate_actions"].items():
            for row in rows:
                reason = None
                if kind not in {"forward_splits", "reverse_splits"}:
                    reason = "unsupported_action_type"
                elif row.get("symbol") != archive["request"]["symbols"]:
                    reason = "symbol_mismatch"
                elif (not isinstance(row.get("id"), str) or not row["id"] or
                      any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] <= 0
                          for k in ("old_rate", "new_rate"))):
                    reason = "invalid_split_identity_or_ratio"
                try:
                    day = date.fromisoformat(row.get("ex_date", ""))
                except (ValueError, TypeError):
                    reason = reason or "effective_date_missing"
                if reason:
                    excluded.append({"id": row.get("id"), "type": kind, "reason": reason})
                    continue
                effective = int(datetime.combine(day, time(), ZoneInfo("America/New_York")).timestamp())
                identity = row["id"]
                if identity in seen:
                    if seen[identity] != row:
                        raise ValueError("Conflicting corporate-action ID")
                    continue
                seen[identity] = row
                events.append({"symbol": row["symbol"], "kind": "split", "effective_ts": effective,
                    "available_ts": effective if allow_effective_date_proxy else observed,
                    "availability_basis": "effective_date_research_assumption" if allow_effective_date_proxy
                                          else "actual_archive_collection",
                    "retrospective": allow_effective_date_proxy, "source": URL, "provider_id": identity,
                    "source_archive_sha256": archive["archive_sha256"], "old_rate": row["old_rate"],
                    "new_rate": row["new_rate"], "action_type": kind})
    events.sort(key=lambda r: (r["effective_ts"], r["provider_id"]))
    result = {"version": "corporate-action-invalidations-v1", "invalidations": events,
              "excluded": excluded, "source_archive_sha256": archive["archive_sha256"],
              "allow_effective_date_proxy": allow_effective_date_proxy, "coverage_complete": False,
              "limitations": ["Only validated forward/reverse splits are normalized so far",
                  "Historical effective-date availability is an assumption, not vendor publication evidence",
                  "No price/share rescaling or complete dilution detection"]}
    result["invalidations_sha256"] = _hash(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-effective-date-proxy", action="store_true")
    args = parser.parse_args()
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    archive, reused = cache_actions(args.symbol, args.start, args.end, args.output,
        key=os.getenv("APCA_API_KEY_ID") or env.get("APCA_API_KEY_ID"),
        secret=os.getenv("APCA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY"))
    result = split_invalidations(archive, allow_effective_date_proxy=args.allow_effective_date_proxy)
    target = args.output / f"{archive['request']['symbols']}_{result['invalidations_sha256'][:16]}_invalidations.json"
    if target.exists():
        if json.loads(target.read_text()) != result:
            raise ValueError("Existing invalidation export differs")
    else:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({"file": str(target), "reused": reused, "rows": archive["rows"],
                         "split_invalidations": len(result["invalidations"]), "excluded": result["excluded"]}))


if __name__ == "__main__":
    main()
