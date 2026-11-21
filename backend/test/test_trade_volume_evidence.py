from datetime import datetime, timezone

import pytest

from drl_agent.historical_features import build_features
from drl_agent.trade_volume_evidence import volume_minutes
from drl_agent.trade_volume_evidence import condition_volume
from test.test_historical_features import fixture, sign
from test.test_synthetic_execution import resign


def evidence(start):
    stamp = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    end = datetime.fromtimestamp(start + 59, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".999999999Z"
    trades = [{"t": stamp, "x": "N", "z": "A", "i": 1, "s": 50, "p": 10, "c": [" ", "I"]},
              {"t": stamp, "x": "N", "z": "A", "i": 2, "s": 10, "p": 10, "c": [" ", "Q"]}]
    result = {"version": "alpaca-trade-gap-v1", "url": "https://data.alpaca.markets/v2/stocks/ABC/trades",
              "request": {"start": stamp, "end": end, "feed": "sip", "asof": "-"},
              "responses": [{"symbol": "ABC", "trades": trades, "next_page_token": None}]}
    resign(result, "archive_sha256")
    return result


def data_with_gap(index):
    data = fixture()
    for candle in data["candles"]:
        candle["source"] = "alpaca_sip"
    start = data["candles"][index]["start_ts"]
    del data["candles"][index]
    del data["states"][index]
    return sign(data), evidence(start)


def test_condition_volume_completes_prior_not_price_history():
    data, artifact = data_with_gap(100)
    before = build_features(data, minimum_sessions=1)
    after = build_features(data, minimum_sessions=1, trade_evidence=[artifact])
    assert before["states"][-1]["rvol"] is None
    assert after["states"][-1]["rvol"] == pytest.approx(39000 / 38950)
    assert len(after["states"]) == len(before["states"])
    assert after["states"][100]["bar_vwap_estimate"] is None
    assert after["states"][100]["minute_macd"] is None
    assert after["prior_session_coverage"][0]["missing_minutes"] == 1
    assert after["prior_session_coverage"][0]["unresolved_volume_minutes"] == 0
    assert after["volume_gap_evidence"][0]["volume"] == 50


def test_current_and_future_evidence_does_not_repair_current_state():
    data, artifact = data_with_gap(400)
    before = build_features(data, minimum_sessions=1)
    after = build_features(data, minimum_sessions=1, trade_evidence=[artifact])
    assert [s["rvol"] for s in before["states"]] == [s["rvol"] for s in after["states"]]
    assert after["states"][-1]["rvol"] is None


def test_existing_bar_cannot_be_double_counted():
    data, artifact = data_with_gap(100)
    other = evidence(data["candles"][0]["start_ts"])
    with pytest.raises(ValueError, match="absent SIP"):
        build_features(data, minimum_sessions=1, trade_evidence=[other])


@pytest.mark.parametrize("conditions", [[" ", "X"], [" ", "I", "unknown"]])
def test_unknown_or_price_forming_trades_rejected(conditions):
    artifact = evidence(1800000000)
    artifact["responses"][0]["trades"][0]["c"] = conditions
    resign(artifact, "archive_sha256")
    with pytest.raises(ValueError, match="conditions"):
        volume_minutes([artifact], "ABC")


def test_corruption_wrong_symbol_and_incomplete_pages_rejected():
    artifact = evidence(1800000000)
    with pytest.raises(ValueError, match="matching"):
        volume_minutes([artifact], "DEF")
    artifact["responses"][0]["next_page_token"] = "unfinished"
    with pytest.raises(ValueError, match="digest"):
        volume_minutes([artifact], "ABC")
    resign(artifact, "archive_sha256")
    with pytest.raises(ValueError, match="pagination"):
        volume_minutes([artifact], "ABC")


@pytest.mark.parametrize("tape,conditions,expected", [
    ("A", {" ", "B"}, True), ("B", {"B"}, True), ("C", {"@", "W"}, True),
    ("C", {"@", "4"}, True), ("A", {"I", "F", "4"}, True),
    ("C", {"G"}, True), ("A", {"P", "Z"}, True),
    ("A", {"I", "M"}, False), ("C", {"Q", "4"}, False), ("B", {"9"}, False),
])
def test_minute_tape_rules(tape, conditions, expected):
    assert condition_volume(tape, conditions) is expected


@pytest.mark.parametrize("tape,conditions", [
    ("C", {"@", "B"}), ("A", {"W", "I"}), ("A", {"G", "I"}),
    ("O", {"I"}), ("C", {" ", "I"}), ("A", {"T"}), ("A", set()),
])
def test_incompatible_tapes_and_price_trades_fail_closed(tape, conditions):
    with pytest.raises(ValueError, match="conditions"):
        condition_volume(tape, conditions)
