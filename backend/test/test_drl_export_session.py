import json
from copy import deepcopy

import pytest

from drl_agent import export_session as module
from drl_agent.partitions import partition_sessions
from test.test_drl_portfolio_replay import observations, WATCH
from test.test_drl_simulator import BASE


def records():
    scanner, market = [], []
    for symbol in ("A", "B"):
        state = observations(symbol)["states"][-1]
        scanner.extend(item["record"] for item in state["inputs"]["scanner"])
        for kind, items in state["inputs"].items():
            if kind != "scanner":
                market.extend(item["record"] for item in items)
    # A nonqualifier must remain part of the recorded candidate universe.
    scanner[1]["payload"]["watch_inputs"]["rvol"] = 0.2
    return scanner, market


def export(tmp_path, scanner, market, **kwargs):
    return module.export_session(tmp_path / "session", scanner_rows=scanner, market_rows=market,
                                 watch_config=WATCH, start_ts=BASE, end_ts=BASE + 3,
                                 step_seconds=1, **kwargs)


def test_all_recorded_symbols_export_aligned_and_pass_hash_checks(tmp_path):
    scanner, market = records()
    before = deepcopy((scanner, market))
    result = export(tmp_path, scanner, market)
    assert result["symbol_count"] == 2
    assert result["coverage"]["B"]["coverage_states"]["scanner"] == 4
    manifest = json.loads((tmp_path / "session" / "manifest.json").read_text())
    datasets = [json.loads((tmp_path / "session" / f).read_text()) for f in manifest["sessions"][0]]
    report = partition_sessions([datasets], watch_config=WATCH, train_end="2026-09-22",
                                validation_end="2026-09-23")
    assert report["partitions"]["train"][0]["symbols"] == ["A", "B"]
    assert report["partitions"]["train"][0]["watch_rejections"]["rvol_below_threshold"] == 4
    assert (scanner, market) == before
    with pytest.raises(FileExistsError):
        export(tmp_path, scanner, market)


def test_missing_market_stays_missing_and_future_only_symbols_are_excluded(tmp_path):
    scanner, _ = records()
    scanner[1]["payload"]["semantic_signal"]["hype"]["as_of_ts"] = BASE + 100
    result = export(tmp_path, scanner, [])
    assert result["symbol_count"] == 1
    coverage = result["coverage"]["A"]["coverage_states"]
    assert coverage == {"scanner": 4, "levelone_quote": 0, "level2_book": 0, "minute_candle": 0}
    assert not result["training_ready"]


def test_symbol_is_not_used_as_a_file_path(tmp_path):
    scanner, _ = records()
    scanner[0]["symbol"] = "../OUTSIDE"
    result = export(tmp_path, scanner[:1], [])
    filename = result["sessions"][0][0]
    assert "/" not in filename and filename.startswith("observations-")
    assert (tmp_path / "session" / filename).exists()


def test_bounds_empty_inputs_and_failed_export_never_publish_manifest(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        module.validate_window(start_ts=BASE, end_ts=BASE + 3, step_seconds=0)
    with pytest.raises(ValueError):
        module.validate_window(start_ts=BASE, end_ts=BASE + 86400, step_seconds=1)
    with pytest.raises(ValueError):
        export(tmp_path, [], [])
    scanner, market = records()
    original = module.build_dataset
    def broken(symbol, **kwargs):
        if symbol == "B":
            raise RuntimeError("simulated write preparation failure")
        return original(symbol, **kwargs)
    monkeypatch.setattr(module, "build_dataset", broken)
    with pytest.raises(RuntimeError):
        export(tmp_path, scanner, market)
    assert not (tmp_path / "session" / "manifest.json").exists()


def test_database_reads_are_one_readonly_snapshot_and_fail_on_truncation(monkeypatch):
    scanner, market = records()
    calls = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, query, params=None):
            calls.append((query, params))
            self.rows = scanner if "FROM feature_store" in query else market
            return self
        def fetchall(self):
            return self.rows
    monkeypatch.setenv("DATABASE_URL", "test-only")
    monkeypatch.setattr(module.psycopg, "connect", lambda *a, **kw: Connection())
    result = module.load_session_records(start_ts=BASE, end_ts=BASE + 3)
    assert result == (scanner, market)
    assert calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert calls[-1][1]["symbols"] == ["A", "B"]
    with pytest.raises(ValueError, match="Input limit"):
        module.load_session_records(start_ts=BASE, end_ts=BASE + 3, max_records=2)
