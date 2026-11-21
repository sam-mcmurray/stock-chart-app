from copy import deepcopy

import pytest

from drl_agent.alpaca_replay_export import build_export, merge_exports, slice_export
from drl_agent.historical_news import build_universe
from drl_agent.historical_symbol_session import build_session
from test.test_alpaca_replay_export import archive
from test.test_synthetic_execution import resign


def test_identical_overlaps_deduplicated_and_order_independent():
    first = build_export(archive())
    second = slice_export(first, first["candles"][0]["start_ts"], first["candles"][100]["end_ts"])
    merged = merge_exports([first, second])
    assert len(merged["candles"]) == len(first["candles"])
    assert merged == merge_exports([second, first])
    assert len({c["id"] for c in merged["candles"]}) == len(merged["candles"])


def test_conflicting_overlap_rejected():
    first = build_export(archive())
    second = deepcopy(first)
    second["candles"][0]["volume"] += 1
    resign(second, "export_sha256")
    with pytest.raises(ValueError, match="Conflicting"):
        merge_exports([first, second])


def test_daily_session_uses_prior_prices_not_future_prices_for_warmup():
    data = archive()
    prices = build_export(data)
    start = prices["candles"][0]["start_ts"]
    news = build_universe([], start_ts=start, end_ts=start + 86400 * 2, enrichment_cutoff_ts=start + 86400 * 3)
    joined, features = build_session([data], [news], "2026-09-22")
    assert len(joined["candles"]) == 390
    assert joined["source_archives"][0]["archive_sha256"] == data["archive_sha256"]
    assert joined["source_news_shards"] == [news["manifest_sha256"]]
    assert features["warmup"]["candles"] == 390
    assert features["warmup"]["last_end_ts"] < joined["candles"][0]["start_ts"]
    assert features["states"][-1]["prior_session_count"] == 1
    assert features["states"][-1]["rvol"] is None
    assert joined["synthetic_depth"]["snapshots"]
    assert not features["training_ready"]


def test_entire_requested_warmup_without_bars_is_preserved_as_missing():
    data = archive()
    data["responses"][0]["bars"]["ABC"] = data["responses"][0]["bars"]["ABC"][390:]
    data["summary"]["rows"] = 390
    resign(data, "archive_sha256")
    start = 1790000000
    news = build_universe([], start_ts=start, end_ts=start + 86400 * 3, enrichment_cutoff_ts=start + 86400 * 4)
    joined, features = build_session([data], [news], "2026-09-22")
    assert len(joined["candles"]) == 390
    assert features["states"][-1]["prior_session_count"] == 1
    assert features["states"][-1]["complete_prior_session_count"] == 0


def test_single_session_cli_writes_strict_watch_diagnostics(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from dataclasses import asdict
    from drl_agent.historical_symbol_session import main
    from drl_agent.watch_replay import WatchConfig
    data = archive()
    news = build_universe([], start_ts=1790000000, end_ts=1790300000, enrichment_cutoff_ts=1790400000)
    config = WatchConfig(1, 20, 3, 20000000, 50, watch_window_seconds=604800)
    for name, value in (("raw", data), ("news", news), ("config", {"watch_config": asdict(config)}),
        ("progress", {"status": "complete", "completed": [{"file": "news.json", "manifest_sha256": news["manifest_sha256"]}]})):
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    monkeypatch.setattr(sys, "argv", ["historical_symbol_session", "--archive", str(tmp_path / "raw.json"),
        "--corpus", str(tmp_path), "--date", "2026-09-22", "--output", str(tmp_path / "result"),
        "--watch-config", str(tmp_path / "config.json")])
    main()
    result = json.loads(capsys.readouterr().out)
    watch = json.loads((tmp_path / "result/watch.json").read_text())
    assert result["watch_sha256"] == watch["watch_sha256"]
    assert result["entry_ready_states"] == 0 and result["network_calls"] == 0
    assert not result["float_evidence_supplied"] and not result["training_started"]
    assert not any(watch["research_policy"].values())
