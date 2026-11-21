from copy import deepcopy
from datetime import datetime, timezone

import pytest

from drl_agent.watch_replay import WatchConfig, replay_watch_windows

BASE = 1790085600
CONFIG = WatchConfig(1, 20, 3, 20_000_000, 2)


def row(symbol="AAA", ts=BASE, *, received=None, price=10, rvol=4, shares=1_000_000, evidence="news"):
    return {"id": f"{symbol}-{ts}-{received}", "symbol": symbol, "source": "scanner_shadow",
            "ts": ts, "created_at": datetime.fromtimestamp(received or ts, timezone.utc),
            "payload": {"watch_inputs": {"version": "scanner-watch-inputs-v1", "observed_at": ts,
                                         "price": price, "rvol": rvol, "float_shares": shares},
                        "semantic_signal": {"hype": {"as_of_ts": ts,
                                                     "evidence_article_ids": [evidence] if evidence else []}}}}


def replay(rows, *, duration=30, config=CONFIG):
    return replay_watch_windows(rows, start_ts=BASE, end_ts=BASE + duration,
                                step_seconds=10, config=config)


def test_forward_clock_rechecks_candidates_and_filters_without_outcomes():
    rows = [row(), row(ts=BASE + 20, price=100), row("BBB", rvol=2), row("CCC", shares=50_000_000)]
    frames = replay(list(reversed(rows)))["frames"]
    assert [f["decision_ts"] for f in frames] == [BASE, BASE + 10, BASE + 20, BASE + 30]
    assert frames[0]["entered_watch"] == ["AAA", "CCC"]
    assert frames[1]["watching"][0]["symbol"] == "AAA"
    assert not frames[1]["watching"][0]["entry_eligible"]
    assert frames[2]["left_watch"] == [{"symbol": "AAA", "reason": "price_out_of_range"}]
    assert frames[0]["rejected"] == {"BBB": "watch_capacity"}
    assert frames[0]["watching"][1]["entry_block_reasons"] == ["float_above_threshold"]
    modified = deepcopy(rows)
    for r in modified:
        r["payload"]["future_return"] = 1000
        r["payload"]["action"] = "buy"
    assert replay(rows) == replay(modified)


def test_late_arrival_future_signal_and_future_rows_cannot_change_prefix():
    late = row(received=BASE + 15)
    future = row("BBB")
    future["payload"]["semantic_signal"]["hype"]["as_of_ts"] = BASE + 25
    frames = replay([late, future])["frames"]
    assert not frames[0]["watching"] and not frames[1]["watching"]
    assert [c["symbol"] for c in frames[2]["watching"]] == ["AAA"]
    assert len(frames[3]["watching"]) == 2
    assert replay([late])["frames"][:3] == frames[:3]


def test_unknown_float_old_inputs_and_news_absence_fail_closed():
    old = row("OLD")
    old["payload"].pop("watch_inputs")
    report = replay([row(shares=None), row("NO_NEWS", evidence=None), old])
    assert report["frames"][0]["rejected"] == {
        "NO_NEWS": "news_evidence_missing", "OLD": "watch_inputs_missing"}
    assert report["frames"][0]["watching"][0]["entry_block_reasons"] == ["float_unknown"]
    assert not report["manifest"]["training_ready"]
    assert not report["manifest"]["execution_supported"]


def test_capacity_ranks_only_current_rvol_then_symbol():
    report = replay([row("CCC", rvol=5), row("BBB"), row("AAA"), row("AAA", ts=BASE + 10, rvol=6)])
    assert [c["symbol"] for c in report["frames"][0]["watching"]] == ["CCC", "AAA"]
    assert [c["symbol"] for c in report["frames"][1]["watching"]] == ["AAA", "CCC"]
    assert report["frames"][0]["rejected"]["BBB"] == "watch_capacity"


def test_expiry_without_new_data_and_no_renewal_from_same_news():
    config = WatchConfig(1, 20, 3, 20_000_000, 2, watch_window_seconds=20)
    frames = replay([row(), row(ts=BASE + 20), row(ts=BASE + 30, evidence="new-news")], config=config)["frames"]
    assert frames[2]["left_watch"] == [{"symbol": "AAA", "reason": "watch_window_expired"}]
    assert frames[3]["entered_watch"] == ["AAA"]
    assert frames[3]["watching"][0]["watch_started_ts"] == BASE + 30


def test_stale_snapshots_and_out_of_order_events():
    config = WatchConfig(1, 20, 3, 20_000_000, 2, snapshot_max_age_seconds=10)
    frames = replay([row(), row(ts=BASE - 1, received=BASE + 5, price=100)], config=config)["frames"]
    assert frames[1]["watching"]
    assert frames[2]["left_watch"] == [{"symbol": "AAA", "reason": "scanner_stale"}]


def test_dropping_out_of_targets_does_not_reset_same_evidence_window():
    config = WatchConfig(1, 20, 3, 20_000_000, 2, watch_window_seconds=20)
    frames = replay([row(), row(ts=BASE + 10, price=100), row(ts=BASE + 20)], config=config)["frames"]
    assert not frames[1]["watching"]
    assert frames[2]["rejected"]["AAA"] == "watch_window_expired"


def test_bounds_and_input_isolation():
    rows = [row()]
    before = deepcopy(rows)
    assert replay(rows) == replay(rows)
    assert rows == before
    with pytest.raises(ValueError):
        WatchConfig(1, float("nan"), 3, 20_000_000, 2)
    with pytest.raises(ValueError):
        replay_watch_windows(rows, start_ts=BASE, end_ts=BASE + 1000000, step_seconds=1, config=CONFIG)


def test_low_volume_starts_watch_before_momentum_without_resetting_news_window():
    config = WatchConfig(1, 20, 3, 20_000_000, 2, watch_window_seconds=30)
    frames = replay([row(rvol=.5), row(ts=BASE + 10, rvol=2), row(ts=BASE + 20, rvol=4)],
                    config=config)["frames"]
    assert frames[0]["entered_watch"] == ["AAA"]
    assert not frames[0]["watching"][0]["momentum_triggered"]
    assert frames[2]["watching"][0]["momentum_triggered"]
    assert frames[2]["watching"][0]["watch_started_ts"] == BASE
    assert not frames[2]["watching"][0]["entry_eligible"]
    assert frames[3]["rejected"]["AAA"] == "watch_window_expired"
