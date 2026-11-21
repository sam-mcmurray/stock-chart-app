from copy import deepcopy

import pytest

from drl_agent.dataset import build_dataset
from drl_agent.synthetic_depth import DepthConfig, generate_depth


def candles():
    return [{"id": str(i), "start_ts": 1800000000 + i * 60,
             "end_ts": 1800000060 + i * 60, "open": 10 + i,
             "high": 11 + i, "low": 9 + i, "close": 10.5 + i, "volume": 100000}
            for i in range(5)]


def test_repeatable_nonmutating_and_no_future_dependency():
    rows = candles()
    before = deepcopy(rows)
    full = generate_depth("ABC", rows)
    assert full == generate_depth("ABC", rows)
    assert rows == before
    assert full["snapshots"][:2] == generate_depth("ABC", rows[:2])["snapshots"]
    assert full["snapshots"] != generate_depth("ABC", rows, config=DepthConfig(seed=1))["snapshots"]
    assert full["snapshots"] != generate_depth("XYZ", rows)["snapshots"]


def test_book_prices_sizes_flows_and_provenance():
    report = generate_depth("ABC", candles())
    assert report["synthetic"] and report["retrospective"]
    assert not report["training_ready"] and not report["live_activation_eligible"]
    for row in report["snapshots"]:
        assert row["available_ts"] == row["source_bar_end_ts"]
        assert row["bids"][0]["price"] < row["asks"][0]["price"]
        for side, reverse in (("bids", True), ("asks", False)):
            prices = [level["price"] for level in row[side]]
            assert prices == sorted(set(prices), reverse=reverse)
            assert all(level["size"] >= 0 for level in row[side])
            flow = row["scenario_flows"][side]
            assert flow["aggressive_order_shares"] == flow["consumed_shares"] + flow["unmatched_shares"]


def test_synthetic_rows_excluded_from_live_dataset():
    rows = generate_depth("ABC", candles())["snapshots"]
    result = build_dataset("ABC", start_ts=1800000060, end_ts=1800000120,
                           scanner_rows=[], market_rows=rows)
    assert result["manifest"]["excluded"] == {"unsupported_source": 5}
    assert result["manifest"]["coverage_states"]["level2_book"] == 0


def test_zero_volume_does_not_invent_displayed_liquidity():
    rows = candles()
    for row in rows:
        row["volume"] = 0
    report = generate_depth("ABC", rows)
    assert all(level["size"] == 0 for row in report["snapshots"]
               for side in ("bids", "asks") for level in row[side])


def test_cli_writes_labeled_artifact_and_refuses_overwrite(tmp_path, monkeypatch):
    import json
    from drl_agent.synthetic_depth import main
    source, output = tmp_path / "input.json", tmp_path / "scenario.json"
    source.write_text(json.dumps({"symbol": "ABC", "candles": candles(), "config": {"seed": 7}}))
    monkeypatch.setattr("sys.argv", ["synthetic_depth", "--input", str(source), "--output", str(output)])
    main()
    result = json.loads(output.read_text())
    assert result["synthetic"] and result["config"]["seed"] == 7
    with pytest.raises(FileExistsError):
        main()


@pytest.mark.parametrize("change", [{"close": float("nan")}, {"volume": -1},
                                    {"high": 1}, {"end_ts": 1800000000}, {"volume": True}])
def test_invalid_bars_rejected(change):
    rows = candles()
    rows[0].update(change)
    with pytest.raises(ValueError):
        generate_depth("ABC", rows)


def test_duplicate_and_reversed_bars_rejected():
    for rows in ([candles()[0]] * 2, list(reversed(candles()))):
        with pytest.raises(ValueError):
            generate_depth("ABC", rows)


@pytest.mark.parametrize("kwargs", [{"seed": True}, {"levels": 0}, {"tick_size": 0},
                                    {"spread_bps": float("inf")}, {"cancellation_fraction": 2}])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        DepthConfig(**kwargs)
