from copy import deepcopy
import json

import pytest

from drl_agent.synthetic_competition import compare_synthetic_sessions
from test.test_synthetic_execution import fixture, resign


class RoundTrip:
    def __init__(self):
        self.calls = 0

    def __call__(self, obs):
        self.calls += 1
        if self.calls % 7 == 1:
            return dict(side="buy", quantity=1, limit_price=11, stop_price=10)
        if obs["account"]["quantity"]:
            return dict(side="sell", quantity=100, limit_price=9)
        return {}


def sessions():
    return [{"date": day, "joined": fixture(day=day)}
            for day in ("2018-07-16", "2018-07-17", "2018-07-23")]


def test_cash_continuity_weekly_reset_and_round_trips():
    result = compare_synthetic_sessions(sessions(), {"a": RoundTrip, "b": RoundTrip})
    competitor = result["competitors"]["a"]
    first, second, third = competitor["daily"]
    assert competitor["complete"]
    assert first["net_pnl"] < 0
    assert second["starting_equity"] == first["ending_equity"]
    assert third["starting_equity"] == 500
    assert third["capital_adjustment"] == pytest.approx(500 - second["ending_equity"])
    assert third["weekly_capital_reset"]
    assert [w["round_trips"] for w in competitor["weekly"]] == [2, 1]
    assert all(d["round_trips"] == 1 for d in competitor["daily"])
    assert result["competitors"]["stay-out"]["weekly"][0]["net_pnl"] == 0
    assert not result["training_ready"] and result["broker_calls"] == 0
    assert result == compare_synthetic_sessions(sessions(), {"a": RoundTrip, "b": RoundTrip})


def test_unresolved_position_stops_without_weekly_erasure():
    def buy(obs):
        return {} if obs["account"]["quantity"] else dict(side="buy", quantity=1, limit_price=11, stop_price=10)
    result = compare_synthetic_sessions(sessions(), {"buy": lambda: buy, "flat": lambda: lambda obs: {}})
    held = result["competitors"]["buy"]
    assert not held["complete"] and len(held["daily"]) == 1
    assert held["daily"][0]["account"]["quantity"] == 1
    assert held["weekly"][0]["risk_adjusted_score"] is None
    assert "unresolved_position_at_session_end" in held["warnings"]


def test_observation_isolation_and_no_future_article_exposure():
    data = sessions()[:1]
    joined = data[0]["joined"]
    joined["articles"] = [{"candidate_id": "future", "replay_available_ts": joined["states"][-1]["decision_ts"]}]
    resign(joined, "join_sha256")
    before = deepcopy(data)
    def inspect(obs):
        assert not obs["articles"]
        assert "states" not in obs and "snapshots" not in obs
        assert obs["book"]["available_ts"] == obs["state"]["decision_ts"]
        obs["book"]["bids"][0]["price"] = -1
        return {}
    compare_synthetic_sessions(data, {"inspect": lambda: inspect, "flat": lambda: lambda obs: {}})
    assert data == before


def test_missing_bid_does_not_invent_zero_valuation_or_score():
    data = sessions()[:1]
    joined = data[0]["joined"]
    for book in joined["synthetic_depth"]["snapshots"][2:]:
        book["bids"][0]["size"] = 0
    resign(joined["synthetic_depth"], "scenario_sha256")
    resign(joined, "join_sha256")
    result = compare_synthetic_sessions(data, {"buy": RoundTrip, "flat": lambda: lambda obs: {}})
    held = result["competitors"]["buy"]
    assert held["daily"][0]["ending_equity"] is None
    assert held["weekly"][0]["risk_adjusted_score"] is None
    assert not held["complete"]


def test_rejects_bad_dates_and_validates_unreached_artifact():
    data = sessions()
    with pytest.raises(ValueError, match="chronological"):
        compare_synthetic_sessions(data[::-1], {"a": RoundTrip, "b": RoundTrip})
    data[-1]["joined"]["join_sha256"] = "bad"
    with pytest.raises(ValueError):
        compare_synthetic_sessions(data, {"a": RoundTrip, "b": RoundTrip})


def test_cli_local_manifest_preserves_orders_and_refuses_overwrite(tmp_path, monkeypatch):
    from drl_agent.run_synthetic_competition import main
    joined = fixture()
    (tmp_path / "joined.json").write_text(json.dumps(joined))
    manifest = {"sessions": [{"date": "2018-07-16", "joined": "joined.json"}],
                "competitors": {"a": [], "b": []}}
    source, output = tmp_path / "manifest.json", tmp_path / "report.json"
    source.write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", ["run", "--manifest", str(source), "--output", str(output)])
    main()
    report = json.loads(output.read_text())
    assert report["diagnostic_orders"] == manifest["competitors"]
    assert all(c["complete"] for c in report["competitors"].values())
    with pytest.raises(FileExistsError):
        main()


def test_cli_rejects_final_state_order(tmp_path, monkeypatch):
    from drl_agent.run_synthetic_competition import main
    joined = fixture()
    (tmp_path / "joined.json").write_text(json.dumps(joined))
    manifest = {"sessions": [{"date": "2018-07-16", "joined": "joined.json"}],
                "competitors": {"a": [{"decision_ts": joined["states"][-1]["decision_ts"]}], "b": []}}
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", ["run", "--manifest", str(source), "--output", str(tmp_path / "out.json")])
    with pytest.raises(ValueError, match="timestamp"):
        main()
