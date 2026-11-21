import json
from datetime import datetime, timezone

import pytest

from drl_agent.alpaca_replay_export import build_export
from drl_agent.corpus_gap_audit import audit, audit_symbol, signed, calendar
from test.test_alpaca_replay_export import archive
from test.test_synthetic_execution import resign
from test.test_trade_volume_evidence import evidence


def inputs(tmp_path):
    source = archive()
    candles = build_export(source)["candles"]
    start = candles[0]["start_ts"]
    del source["responses"][0]["bars"]["ABC"][100]
    source["summary"]["rows"] -= 1
    resign(source, "archive_sha256")
    (tmp_path / "ABC.json").write_text(json.dumps(source))
    sessions = [{"date": date, "open_ts": low, "close_ts": low + 390 * 60}
                for date, low in [("2026-09-21", start), ("2026-09-22", start + 86400)]]
    cal = signed({"version": "historical-gap-calendar-v1", "start": "2026-09-21", "end": "2026-09-22",
                  "sessions": sessions}, "calendar_sha256")
    request = {"request_id": "request", "symbol": "ABC", "start_ts": start, "end_ts": start + 86400 + 390*60}
    plan = signed({"version": "historical-corpus-price-plan-v1", "requests": [request], "reusable_archives": []}, "plan_sha256")
    receipt = {"status": "complete", "plan_sha256": plan["plan_sha256"], "completed": {
        "request": {"status": "cached", "file": "ABC.json", "archive_sha256": source["archive_sha256"]}}}
    return source, cal, plan, receipt, start


def test_regular_session_gaps_and_resume(tmp_path):
    source, cal, plan, receipt, start = inputs(tmp_path)
    out = tmp_path / "audit"
    report = audit(plan, receipt, cal, tmp_path, tmp_path / "trades", out)
    assert report["status"] == "complete" and report["uncached_minutes"] == 1
    symbol = json.loads((out / "ABC.json").read_text())
    assert symbol["uncached_minutes"] == [start + 6000]
    assert audit(plan, receipt, cal, tmp_path, tmp_path / "trades", out)["new_symbols_this_run"] == 0


def test_partial_request_session_is_not_a_gap_download_task(tmp_path):
    source, cal, _, _, start = inputs(tmp_path)
    report = audit_symbol("ABC", [{"file": "ABC.json", "archive_sha256": source["archive_sha256"]}],
                          [(start+120, start+86400+390*60)], cal, tmp_path, {})
    assert len(report["sessions"]) == 1
    assert report["missing_price_minutes"] == 0


def test_corrupt_source_fails_and_records_failure(tmp_path):
    _, cal, plan, receipt, _ = inputs(tmp_path)
    path = tmp_path / "ABC.json"
    changed = json.loads(path.read_text())
    changed["summary"]["rows"] += 1
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="digest"):
        audit(plan, receipt, cal, tmp_path, tmp_path / "trades", tmp_path / "audit")
    assert json.loads((tmp_path / "audit/progress.json").read_text())["status"] == "failed"


def test_no_data_symbol_included_and_batch_resumes(tmp_path):
    _, cal, plan, receipt, start = inputs(tmp_path)
    plan.pop("plan_sha256")
    plan["requests"].append({"request_id": "empty", "symbol": "DEF", "start_ts": start, "end_ts": start + 390*60})
    signed(plan, "plan_sha256")
    receipt["plan_sha256"] = plan["plan_sha256"]
    receipt["completed"]["empty"] = {"status": "no_data"}
    report = audit(plan, receipt, cal, tmp_path, tmp_path / "trades", tmp_path / "audit", max_symbols=1)
    assert report["status"] == "paused_at_batch_limit"
    report = audit(plan, receipt, cal, tmp_path, tmp_path / "trades", tmp_path / "audit", max_symbols=1)
    assert report["status"] == "complete" and report["uncached_minutes"] == 391


def test_calendar_weekend_bounds_and_exchange_holiday():
    result = calendar("2026-07-01", "2026-07-05")
    assert [s["date"] for s in result["sessions"]] == ["2026-07-01", "2026-07-02"]


def test_cached_evidence_reused_and_snapshot_frozen(tmp_path):
    _, cal, plan, receipt, start = inputs(tmp_path)
    trades = tmp_path / "trades"
    trades.mkdir()
    stamp = start + 6000
    name = f"ABC_{datetime.fromtimestamp(stamp, timezone.utc):%Y%m%dT%H%MZ}_sip_trades.json"
    path = trades / name
    artifact = evidence(stamp)
    path.write_text(json.dumps(artifact))
    out = tmp_path / "audit"
    report = audit(plan, receipt, cal, tmp_path, trades, out)
    assert report["verified_volume_minutes"] == 1 and report["uncached_minutes"] == 0
    artifact["responses"][0]["trades"][0]["s"] += 1
    resign(artifact, "archive_sha256")
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="Frozen"):
        audit(plan, receipt, cal, tmp_path, trades, out)
