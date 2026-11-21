from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from drl_agent.dataset import encode_dataset
from drl_agent.historical_features import build_features


def sign(data):
    data.pop("join_sha256", None)
    data["join_sha256"] = hashlib.sha256(encode_dataset(data).encode()).hexdigest()
    return data


def fixture():
    candles, states = [], []
    for day in ("2026-09-21", "2026-09-22"):
        start = int(pd.Timestamp(day + "T13:30Z").timestamp())
        for i in range(390):
            identity = f"{day}-{i}"
            candles.append({"id": identity, "start_ts": start + i * 60, "end_ts": start + (i + 1) * 60,
                            "high": 11., "low": 9., "close": 10., "volume": 100., "source": "pitrading"})
            states.append({"candle_id": identity, "decision_ts": start + (i + 1) * 60,
                           "news_candidate_ids": [], "news_watch_active": False})
    return sign({"version": "historical-news-price-join-v1", "symbol": "ABC", "candles": candles,
                 "states": states, "limitations": []})


def test_prior_day_baseline_and_no_today_lookahead():
    data = fixture()
    result = build_features(data, minimum_sessions=1)
    assert result["states"][389]["rvol"] is None
    assert result["states"][390]["rvol"] == pytest.approx(1 / 390)
    assert result["states"][-1]["rvol"] == 1
    assert result["states"][0]["bar_vwap_estimate"] == 10
    assert result["states"][38]["minute_macd"] is None
    assert result["states"][39]["minute_macd"] == 0
    short = deepcopy(data)
    short["candles"], short["states"] = short["candles"][:410], short["states"][:410]
    assert build_features(sign(short), minimum_sessions=1)["states"] == result["states"][:410]


def test_partial_request_boundary_excluded_but_full_request_gaps_retained():
    data = fixture()
    opening = data["candles"][0]["start_ts"]
    data["candles"], data["states"] = data["candles"][100:], data["states"][100:]
    data["requested_start_ts"] = opening + 6000
    partial = build_features(sign(data), minimum_sessions=1)
    assert partial["excluded_partial_request_sessions"] == [opening]
    assert partial["states"][-1]["prior_session_count"] == 0
    assert partial["states"][-1]["rvol_unavailable_reason"] == "insufficient_prior_sessions"
    data["requested_start_ts"] = opening
    full = build_features(sign(data), minimum_sessions=1)
    assert full["excluded_partial_request_sessions"] == []
    assert full["prior_session_coverage"][0]["missing_minutes"] == 100
    assert full["states"][-1]["rvol_unavailable_reason"] == "incomplete_prior_sessions"


def test_requested_entire_missing_day_is_not_hidden_by_first_returned_bar():
    data = fixture()
    opening = data["candles"][0]["start_ts"]
    data["candles"], data["states"] = data["candles"][390:], data["states"][390:]
    data["requested_start_ts"] = opening
    result = build_features(sign(data), minimum_sessions=1)
    assert result["prior_session_coverage"][0]["observed_minutes"] == 0
    assert result["states"][-1]["prior_session_count"] == 1
    assert result["states"][-1]["rvol"] is None


def test_complete_sessions_after_partial_boundary_can_establish_baseline():
    data = fixture()
    extra_candles, extra_states = deepcopy(data["candles"][390:]), deepcopy(data["states"][390:])
    for candle, state in zip(extra_candles, extra_states):
        candle["start_ts"] += 86400
        candle["end_ts"] += 86400
        candle["id"] += "-next"
        state["decision_ts"] += 86400
        state["candle_id"] = candle["id"]
    data["candles"] = data["candles"][100:] + extra_candles
    data["states"] = data["states"][100:] + extra_states
    data["requested_start_ts"] = data["candles"][0]["start_ts"]
    result = build_features(sign(data), minimum_sessions=1)
    assert result["states"][-1]["rvol"] == 1
    assert result["states"][-1]["complete_prior_session_count"] == 1


def test_missing_prior_minutes_do_not_supply_rvol_baseline():
    data = fixture()
    del data["candles"][100]
    del data["states"][100]
    result = build_features(sign(data), minimum_sessions=1)
    assert result["states"][-1]["complete_prior_session_count"] == 0
    assert result["states"][-1]["rvol"] is None
    assert result["states"][100]["bar_vwap_estimate"] is None
    first_next_day = result["states"][389]
    assert first_next_day["observed_volume_rvol"] == pytest.approx(1 / 389)
    assert first_next_day["prior_minute_coverage"] == pytest.approx(389 / 390)
    assert first_next_day["missing_prior_minutes"] == 1
    assert first_next_day["rvol_unavailable_reason"] == "incomplete_prior_sessions"


def test_observed_current_volume_is_not_scaled_to_fill_missing_minutes():
    data = fixture()
    del data["candles"][391]
    del data["states"][391]
    result = build_features(sign(data), minimum_sessions=1)
    state = result["states"][391]
    assert state["observed_session_volume"] == 200
    assert state["observed_volume_rvol"] == pytest.approx(200 / 39000)
    assert state["current_minute_coverage"] == pytest.approx(2 / 3)
    assert state["missing_current_minutes"] == 1
    assert state["rvol"] is None


def test_entirely_missing_prior_session_blocks_observed_volume_rvol():
    data = fixture()
    for candle, state in zip(data["candles"][390:], data["states"][390:]):
        candle["start_ts"] += 86400
        candle["end_ts"] += 86400
        state["decision_ts"] += 86400
    result = build_features(sign(data), minimum_sessions=1)
    state = result["states"][390]
    assert state["prior_session_count"] == 2
    assert state["observed_prior_session_count"] == 1
    assert state["missing_prior_minutes"] == 390
    assert state["observed_volume_rvol"] is None


def test_default_requires_twenty_complete_sessions_and_keeps_eligibility_unknown():
    result = build_features(fixture())
    assert result["coverage"]["rvol_states"] == 0
    assert result["coverage"]["observed_volume_rvol_states"] == 0
    assert all(s["float_shares"] is None and not s["entry_eligible"] for s in result["states"])


def test_mixed_sources_and_digest_tampering_rejected():
    data = fixture()
    data["candles"][-1]["source"] = "iex"
    with pytest.raises(ValueError, match="digest"):
        build_features(data)
    with pytest.raises(ValueError, match="mix"):
        build_features(sign(data))


def warmup_fixture():
    full = fixture()
    full["source_file_sha256"] = "archive"
    replay = deepcopy(full)
    replay["candles"], replay["states"] = replay["candles"][390:], replay["states"][390:]
    chunk = {"version": "hf-historical-replay-inputs-v1", "symbol": "ABC",
             "source_file_sha256": "archive", "candles": full["candles"][:390]}
    chunk["export_sha256"] = hashlib.sha256(encode_dataset(chunk).encode()).hexdigest()
    return sign(full), sign(replay), chunk


def test_warmup_matches_long_replay_without_adding_decisions():
    full, replay, chunk = warmup_fixture()
    before = deepcopy(replay)
    result = build_features(replay, minimum_sessions=1, warmup=[chunk])
    assert result["states"] == build_features(full, minimum_sessions=1)["states"][390:]
    assert result["coverage"]["states"] == 390
    assert result["coverage"]["rvol_states"] == 390
    assert result["warmup"]["candles"] == 390
    assert result["source_join_sha256"] == replay["join_sha256"]
    assert replay == before


@pytest.mark.parametrize("change", ["archive", "symbol", "future", "duplicate"])
def test_warmup_rejects_wrong_identity_future_and_duplicate_candles(change):
    _, replay, chunk = warmup_fixture()
    if change == "archive":
        chunk["source_file_sha256"] = "different"
    elif change == "symbol":
        chunk["symbol"] = "XYZ"
    elif change == "future":
        chunk["candles"].append(replay["candles"][0])
    else:
        chunk["candles"].append(chunk["candles"][0])
    chunk.pop("export_sha256")
    chunk["export_sha256"] = hashlib.sha256(encode_dataset(chunk).encode()).hexdigest()
    with pytest.raises(ValueError):
        build_features(replay, minimum_sessions=1, warmup=[chunk])
