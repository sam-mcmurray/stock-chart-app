"""Resume a frozen price plan through the existing market-data-only downloader."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from dotenv import dotenv_values

from drl_agent.alpaca_data_cache import cache_bars, NoBarsError, MarketDataHTTPError
from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def acquire(plan, cache, output, *, max_requests=20, key=None, secret=None,
            fetch=cache_bars, pause=time.sleep, notify=None):
    _verify(plan, "historical-corpus-price-plan-v1", "plan_sha256")
    if type(max_requests) is not int or not 1 <= max_requests <= 10000:
        raise ValueError("max_requests must be 1..10000")
    requests, seen = plan["requests"], set()
    for row in requests:
        start, end = row["start_ts"], row["end_ts"]
        identity = hashlib.sha256(encode_dataset([row["symbol"], start, end, "sip", "raw"]).encode()).hexdigest()
        if (row["request_id"] != identity or identity in seen
                or type(start) is not int or type(end) is not int
                or start <= 0 or start % 60 or end % 60 or not 0 < end - start <= 31 * 86400):
            raise ValueError("Invalid or duplicate acquisition request")
        seen.add(identity)
    output, cache = Path(output), Path(cache)
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    with (output / ".acquire.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = output / "progress.json"
        report = json.loads(path.read_text()) if path.exists() else {
            "version": "historical-price-acquisition-v1", "plan_sha256": plan["plan_sha256"],
            "completed": {}, "training_started": False, "broker_calls": 0}
        if report.get("plan_sha256") != plan["plan_sha256"] or not set(report["completed"]) <= seen:
            raise ValueError("Acquisition progress belongs to another plan")

        def save(status):
            report.update(status=status, updated_at=datetime.now(timezone.utc).isoformat(),
                          total_requests=len(requests), remaining=len(requests) - len(report["completed"]))
            pending = path.with_suffix(".tmp")
            pending.write_text(encode_dataset(report) + "\n")
            pending.replace(path)

        # Verify completed caches before skipping; never redownload corrupted/missing evidence.
        for result in report["completed"].values():
            if result["status"] == "no_data":
                continue
            archive = json.loads((cache / result["file"]).read_text())
            _verify(archive, "alpaca-sip-raw-cache-v1", "archive_sha256")
            if archive["archive_sha256"] != result["archive_sha256"]:
                raise ValueError("Completed cache changed")
        report.pop("error_type", None)
        report.pop("http_status", None)
        report["attempted_this_run"] = 0
        save("running")
        for row in sorted(requests, key=lambda r: (-r["end_ts"], -r["start_ts"], r["symbol"])):
            identity = row["request_id"]
            if identity in report["completed"]:
                continue
            if report["attempted_this_run"] >= max_requests:
                break
            report.update(current_request=identity, current_symbol=row["symbol"])
            save("running")
            try:
                result = fetch(row["symbol"], datetime.fromtimestamp(row["start_ts"], timezone.utc).isoformat(),
                               datetime.fromtimestamp(row["end_ts"], timezone.utc).isoformat(), cache,
                               key=key, secret=secret)
                result = {**result, "file": Path(result["file"]).name, "status": "cached"}
            except NoBarsError:
                result = {"status": "no_data", "symbol": row["symbol"],
                          "observed_at": datetime.now(timezone.utc).isoformat()}
            except Exception as exc:
                report["error_type"] = type(exc).__name__
                if isinstance(exc, MarketDataHTTPError):
                    report["http_status"] = exc.status_code
                save("failed")
                raise RuntimeError("Acquisition stopped; see sanitized progress and retry after diagnosis") from None
            report["completed"][identity] = result
            report["attempted_this_run"] += 1
            save("running")
            if notify:
                notify({"symbol": row["symbol"], "status": result["status"], "rows": result.get("rows", 0),
                        "completed": len(report["completed"]), "remaining": report["remaining"]})
            pause(1)
        save("complete" if len(report["completed"]) == len(requests) else "paused_at_batch_limit")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=20)
    args = parser.parse_args()
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    report = acquire(json.loads(args.plan.read_text()), args.cache, args.output,
                     max_requests=args.max_requests,
                     key=os.getenv("APCA_API_KEY_ID") or env.get("APCA_API_KEY_ID"),
                     secret=os.getenv("APCA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY"),
                     notify=lambda row: print(encode_dataset(row), flush=True))
    print(encode_dataset({k: v for k, v in report.items() if k != "completed"}), flush=True)


if __name__ == "__main__":
    main()
