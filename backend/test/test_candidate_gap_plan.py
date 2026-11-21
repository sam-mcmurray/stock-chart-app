import json

import pytest

from drl_agent.alpaca_trade_gap import cache_minute
from drl_agent.candidate_gap_plan import candidates, build_plan, verify
from drl_agent.candidate_gap_plan import repair_plan
from drl_agent.corpus_gap_audit import audit, signed
from drl_agent.watch_replay import WatchConfig
from test.test_alpaca_data_cache import Response
from test.test_corpus_gap_audit import inputs


def config():
    return WatchConfig(1, 20, 3, 20000000, 50, watch_window_seconds=604800)


def test_causal_news_price_admission_not_future_move():
    session = {"open_ts": 100, "close_ts": 1000, "date": "day"}
    bars = [{"start_ts": 100, "end_ts": 160, "close": 10},
            {"start_ts": 160, "end_ts": 220, "close": 30},
            {"start_ts": 220, "end_ts": 280, "close": 12}]
    result = candidates(bars, [{"replay_available_ts": 200}], [session], config())
    assert result[0]["watch_states"] == 1
    assert candidates(bars, [{"replay_available_ts": 900}], [session], config()) == []
    assert candidates(bars, [{"replay_available_ts": -604800}], [session], config()) == []


def test_plan_keeps_prior_gap_for_news_price_candidate(tmp_path):
    _, cal, prices, receipts, start = inputs(tmp_path)
    audit_dir = tmp_path / "audit"
    audit(prices, receipts, cal, tmp_path, tmp_path / "evidence", audit_dir)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shard = signed({"version": "retrospective-news-universe-v1", "candidates": [
        {"tickers": ["ABC"], "replay_available_ts": start+86400}]}, "manifest_sha256")
    (corpus / "day.json").write_text(json.dumps(shard))
    (corpus / "progress.json").write_text(json.dumps({"status": "complete", "completed": [
        {"file": "day.json", "manifest_sha256": shard["manifest_sha256"]}]}))
    plan = build_plan(corpus, audit_dir, tmp_path, cal, config())
    assert len(plan["candidates"]) == 1
    assert plan["candidates"][0]["uncached_prior_minutes"] == [start+6000]
    assert len(plan["candidates"][0]["sessions"]) == 1
    assert plan["network_calls"] == 0


def verification_plan():
    return signed({"version": "candidate-gap-plan-v1", "candidates": [
        {"symbol": "ABC", "uncached_prior_minutes": [1781703000, 1781703060]}]}, "plan_sha256")


def test_repair_plan_bounds_newest_leads_and_reuses_verified_cache(tmp_path):
    from test.test_trade_volume_evidence import evidence
    artifact = evidence(1781703000)
    (tmp_path / "minute.json").write_text(json.dumps(artifact))
    rows = [{"symbol": s, "sessions": [{"date": day, "open_ts": opening}],
             "uncached_prior_minutes": minutes} for s, day, opening, minutes in [
        ("ABC", "2026-06-18", 1781789400, [1781703000, 1781703060, 1781789400]),
        ("DEF", "2026-06-18", 1781789400, [1781703000, 1781703060]),
        ("GHI", "2026-06-17", 1781703000, [1781616600]),
        ("JKL", "2026-06-18", 1781789400, [1781703000])]]
    plan = signed({"version": "candidate-gap-plan-v1", "candidates": rows}, "plan_sha256")
    readiness = signed({"version": "candidate-session-readiness-v1", "plan_sha256": plan["plan_sha256"],
        "volume_repair_sessions": [{"symbol": r["symbol"], "date": r["sessions"][0]["date"]}
                                   for r in rows[:3]]}, "report_sha256")
    receipts = {"plan_sha256": plan["plan_sha256"], "completed": {"ABC:1781703000": {
        "file": "minute.json", "archive_sha256": artifact["archive_sha256"], "status": "unsupported_conditions"}}}
    result = repair_plan(plan, readiness, receipts, tmp_path, max_minutes=2)
    assert [r["symbol"] for r in result["candidates"]] == ["ABC", "DEF"]
    assert [r["uncached_prior_minutes"] for r in result["candidates"]] == [[1781703060], [1781703060]]
    assert result["source_plan_sha256"] == plan["plan_sha256"] and result["network_calls"] == 0
    assert rows[0]["uncached_prior_minutes"] == [1781703000, 1781703060, 1781789400]
    cached = evidence(1781703060)
    (tmp_path / "ABC_20260617T1331Z_sip_trades.json").write_text(json.dumps(cached))
    smaller = repair_plan(plan, readiness, receipts, tmp_path, max_minutes=2, max_symbol_minutes=1)
    assert [r["symbol"] for r in smaller["candidates"]] == ["GHI"]
    assert smaller["skipped_larger_repairs"] == {"DEF": 2}
    assert cached["archive_sha256"] in smaller["source_trade_hashes"]
    with pytest.raises(ValueError, match="per-symbol"):
        repair_plan(plan, readiness, receipts, tmp_path, max_symbol_minutes=0)
    artifact["responses"][0]["trades"][0]["s"] += 1
    (tmp_path / "minute.json").write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="digest"):
        repair_plan(plan, readiness, receipts, tmp_path, max_minutes=2)


def test_verification_bounded_resume_and_cache_reuse(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs["params"]["start"])
        return Response({"symbol": "ABC", "trades": []})
    def fetch(symbol, start, output, **kwargs):
        return cache_minute(symbol, start, output, key="k", secret="s", get=get)
    first = verify(verification_plan(), tmp_path / "cache", tmp_path / "run", max_requests=1,
                   fetch=fetch, pause=lambda _: None)
    assert first["status"] == "paused_at_batch_limit" and first["remaining"] == 1
    second = verify(verification_plan(), tmp_path / "cache", tmp_path / "run", fetch=fetch, pause=lambda _: None)
    assert second["status"] == "complete" and len(calls) == 2
    assert calls == sorted(calls, reverse=True)
    verify(verification_plan(), tmp_path / "cache", tmp_path / "run",
           fetch=lambda *a, **k: pytest.fail("Network reused"))


def test_provider_error_stops_without_completed_receipt(tmp_path):
    def fetch(symbol, start, output, **kwargs):
        return cache_minute(symbol, start, output, key="k", secret="s", get=lambda *a, **k: Response({}, 429))
    with pytest.raises(RuntimeError, match="stopped"):
        verify(verification_plan(), tmp_path / "cache", tmp_path / "run",
               fetch=fetch)
    progress = json.loads((tmp_path / "run/progress.json").read_text())
    assert progress["status"] == "failed" and not progress["completed"]
    assert progress["http_status"] == 429


def test_cached_reclassification_no_download_and_checksum_guard(tmp_path):
    from test.test_trade_volume_evidence import evidence
    from test.test_synthetic_execution import resign
    from drl_agent.trade_volume_evidence import VOLUME_POLICY
    cache, output = tmp_path / "cache", tmp_path / "run"
    cache.mkdir()
    output.mkdir()
    artifact = evidence(1781703000)
    artifact["responses"][0]["trades"][0]["c"] = [" ", "4"]
    resign(artifact, "archive_sha256")
    raw = json.dumps(artifact)
    (cache / "minute.json").write_text(raw)
    progress = {"plan_sha256": verification_plan()["plan_sha256"], "completed": {
        "ABC:1781703000": {"file": "minute.json", "archive_sha256": artifact["archive_sha256"],
                           "status": "unsupported_conditions", "volume": None}}}
    (output / "progress.json").write_text(json.dumps(progress))
    result = verify(verification_plan(), cache, output, cached_only=True,
                    fetch=lambda *a, **k: pytest.fail("Offline reclassification downloaded"))
    row = result["completed"]["ABC:1781703000"]
    assert row["status"] == "volume_verified" and row["volume"] == 50
    assert row["policy"] == VOLUME_POLICY and result["remaining"] == 1
    assert (cache / "minute.json").read_text() == raw
    artifact["responses"][0]["trades"][0]["s"] = 100
    (cache / "minute.json").write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="digest"):
        verify(verification_plan(), cache, output, cached_only=True)


def test_cached_discovery_skips_missing_minutes_and_never_downloads(tmp_path):
    from test.test_trade_volume_evidence import evidence
    cache = tmp_path / "cache"
    cache.mkdir()
    artifact = evidence(1781703000)
    target = cache / "ABC_20260617T1330Z_sip_trades.json"
    raw = json.dumps(artifact)
    target.write_text(raw)
    # The newer minute is absent: discovery must continue to the older cache.
    result = verify(verification_plan(), cache, tmp_path / "run", cached_only=True,
                    fetch=lambda *a, **k: pytest.fail("Offline discovery downloaded"),
                    pause=lambda _: pytest.fail("Offline discovery throttled"))
    assert list(result["completed"]) == ["ABC:1781703000"]
    assert result["completed"]["ABC:1781703000"]["status"] == "volume_verified"
    assert result["remaining"] == 1 and result["status"] == "cache_exhausted"
    assert target.read_text() == raw
    artifact["responses"][0]["trades"][0]["s"] += 1
    target.write_text(json.dumps(artifact))
    with pytest.raises(RuntimeError, match="stopped"):
        verify(verification_plan(), cache, tmp_path / "corrupt_run", cached_only=True)
    failed = json.loads((tmp_path / "corrupt_run/progress.json").read_text())
    assert failed["status"] == "failed" and not failed["completed"]


def test_verify_cli_prints_completed_summary(tmp_path, monkeypatch, capsys):
    import sys
    from drl_agent.candidate_gap_plan import main
    plan = signed({"version": "candidate-gap-plan-v1", "candidates": []}, "plan_sha256")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(sys, "argv", ["candidate_gap_plan", "verify", "--plan", str(path),
        "--cache", str(tmp_path / "cache"), "--output", str(tmp_path / "run")])
    main()
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_cached_discovery_ignores_request_budget_and_unplanned_files(tmp_path):
    from test.test_trade_volume_evidence import evidence
    cache = tmp_path / "cache"
    cache.mkdir()
    for stamp, minute in ((1781703000, "1330"), (1781703060, "1331")):
        (cache / f"ABC_20260617T{minute}Z_sip_trades.json").write_text(json.dumps(evidence(stamp)))
    (cache / "UNPLANNED_20260617T1330Z_sip_trades.json").write_text("not a planned artifact")
    result = verify(verification_plan(), cache, tmp_path / "run", max_requests=1, cached_only=True,
                    fetch=lambda *a, **k: pytest.fail("Offline discovery downloaded"))
    assert result["status"] == "complete" and result["remaining"] == 0
    assert len(result["completed"]) == 2


def cached_verification_inputs(tmp_path, count):
    from datetime import datetime, timezone
    from test.test_trade_volume_evidence import evidence
    cache = tmp_path / "cache"
    cache.mkdir()
    minutes = [1781703000 + i * 60 for i in range(count)]
    files = {}
    for stamp in minutes:
        minute = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y%m%dT%H%MZ")
        path = cache / f"ABC_{minute}_sip_trades.json"
        path.write_text(json.dumps(evidence(stamp)))
        files[stamp] = path
    plan = signed({"version": "candidate-gap-plan-v1", "candidates": [
        {"symbol": "ABC", "uncached_prior_minutes": minutes}]}, "plan_sha256")
    return plan, cache, files


@pytest.mark.parametrize("cached_only", [True, False])
def test_verification_checkpoints_are_batched_only_offline(tmp_path, monkeypatch, cached_only):
    from datetime import datetime
    import drl_agent.candidate_gap_plan as module
    plan, cache, files = cached_verification_inputs(tmp_path, 205)
    output = tmp_path / "run"
    snapshots, fetched = [], []
    original_save = module.save
    def save(path, progress):
        snapshots.append((progress["status"], len(progress["completed"])))
        original_save(path, progress)
    monkeypatch.setattr(module, "save", save)
    def fetch(symbol, start, destination, **kwargs):
        assert not cached_only, "Offline verification downloaded"
        assert len(json.loads((output / "progress.json").read_text())["completed"]) == len(fetched)
        stamp = int(datetime.fromisoformat(start).timestamp())
        fetched.append(stamp)
        return {"file": files[stamp]}
    result = verify(plan, cache, output, max_requests=205, cached_only=cached_only,
                    fetch=fetch, pause=lambda _: None)
    assert result["status"] == "complete" and result["remaining"] == 0
    assert len(result["completed"]) == 205
    assert all(row["status"] == "volume_verified" for row in result["completed"].values())
    assert json.loads((output / "progress.json").read_text()) == result
    if cached_only:
        assert snapshots == [("running", 0), ("running", 100), ("running", 200), ("complete", 205)]
        assert not fetched
    else:
        assert len(snapshots) == 411 and len(fetched) == 205
        assert snapshots[-1] == ("complete", 205)


def test_cached_failure_flushes_uncheckpointed_receipts_and_resumes(tmp_path):
    plan, cache, files = cached_verification_inputs(tmp_path, 3)
    oldest = files[min(files)]
    raw = oldest.read_text()
    oldest.write_text("invalid cached JSON")
    output = tmp_path / "run"
    def no_fetch(*args, **kwargs):
        pytest.fail("Offline verification downloaded")
    with pytest.raises(RuntimeError, match="stopped"):
        verify(plan, cache, output, cached_only=True, fetch=no_fetch)
    failed = json.loads((output / "progress.json").read_text())
    assert failed["status"] == "failed" and failed["error_type"] == "JSONDecodeError"
    assert len(failed["completed"]) == 2 and failed["remaining"] == 1
    oldest.write_text(raw)
    resumed = verify(plan, cache, output, cached_only=True, fetch=no_fetch)
    assert resumed["status"] == "complete" and len(resumed["completed"]) == 3
    assert "error_type" not in resumed


def test_cached_interruption_revalidates_uncheckpointed_evidence(tmp_path):
    plan, cache, _ = cached_verification_inputs(tmp_path, 3)
    output = tmp_path / "run"
    def interrupt(update):
        raise KeyboardInterrupt
    def no_fetch(*args, **kwargs):
        pytest.fail("Offline verification downloaded")
    with pytest.raises(KeyboardInterrupt):
        verify(plan, cache, output, cached_only=True, notify=interrupt, fetch=no_fetch)
    checkpoint = json.loads((output / "progress.json").read_text())
    assert checkpoint["status"] == "running" and not checkpoint["completed"]
    checked = []
    result = verify(plan, cache, output, cached_only=True, notify=checked.append, fetch=no_fetch)
    assert result["status"] == "complete" and len(checked) == 3
    assert json.loads((output / "progress.json").read_text()) == result
