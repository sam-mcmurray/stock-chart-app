import pytest
import json

from drl_agent.synthetic_portfolio_competition import compare_synthetic_portfolio_sessions
from test.test_synthetic_execution import fixture
from test.test_synthetic_portfolio import BUY, SELL


class RoundTrip:
    def __init__(self):
        self.calls = 0

    def __call__(self, obs):
        self.calls += 1
        if self.calls % 7 == 1:
            return {s: BUY for s in obs["symbols"]}
        return {s: SELL for s in obs["account"]["positions"]}


def sessions():
    return [{"date": day, "joined": [fixture(day=day, symbol=s) for s in "AB"]}
            for day in ("2018-07-16", "2018-07-17", "2018-07-23")]


def test_weekly_shared_cash_and_round_trip_comparison():
    result = compare_synthetic_portfolio_sessions(sessions(), {"a": RoundTrip, "b": RoundTrip})
    a = result["competitors"]["a"]
    first, second, third = a["daily"]
    assert a["complete"]
    assert first["starting_equity"] == 500
    assert second["starting_equity"] == first["ending_equity"]
    assert third["starting_equity"] == 500 and third["weekly_capital_reset"]
    assert third["capital_adjustment"] == pytest.approx(500 - second["ending_equity"])
    assert [w["round_trips"] for w in a["weekly"]] == [4, 2]
    assert result["competitors"]["stay-out"]["weekly"][0]["net_pnl"] == 0
    assert result == compare_synthetic_portfolio_sessions(sessions(), {"a": RoundTrip, "b": RoundTrip})
    assert not result["promotion_allowed"] and result["broker_calls"] == 0


def test_unresolved_inventory_blocks_rollover_and_score():
    def hold(obs):
        return {"A": BUY} if not obs["account"]["positions"] else {}
    result = compare_synthetic_portfolio_sessions(sessions(), {"hold": lambda: hold, "flat": lambda: lambda obs: {}})
    held = result["competitors"]["hold"]
    assert not held["complete"] and len(held["daily"]) == 1
    assert held["weekly"][0]["risk_adjusted_score"] is None
    assert held["daily"][0]["replay"]["account"]["positions"]["A"]["quantity"] > 0


def test_rejects_bad_calendar_order_and_unreached_bad_artifact():
    data = sessions()
    with pytest.raises(ValueError, match="chronological"):
        compare_synthetic_portfolio_sessions(data[::-1], {"a": RoundTrip, "b": RoundTrip})
    data[-1]["joined"][0]["join_sha256"] = "bad"
    with pytest.raises(ValueError):
        compare_synthetic_portfolio_sessions(data, {"a": RoundTrip, "b": RoundTrip})


def test_portfolio_cli_multiple_orders_at_same_timestamp(tmp_path, monkeypatch):
    from drl_agent.run_synthetic_competition import main
    schedules = []
    for symbol in "AB":
        joined = fixture(symbol=symbol)
        (tmp_path / f"{symbol}.json").write_text(json.dumps(joined))
        schedules.extend([{**BUY, "symbol": symbol, "decision_ts": joined["states"][0]["decision_ts"]},
                          {**SELL, "symbol": symbol, "decision_ts": joined["states"][1]["decision_ts"]}])
    manifest = {"sessions": [{"date": "2018-07-16", "joined": ["A.json", "B.json"]}],
                "competitors": {"trade": schedules, "flat": []}}
    source, output = tmp_path / "manifest.json", tmp_path / "report.json"
    source.write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", ["run", "--portfolio", "--manifest", str(source), "--output", str(output)])
    main()
    report = json.loads(output.read_text())
    assert report["competitors"]["trade"]["weekly"][0]["round_trips"] == 2
    assert report["diagnostic_orders"]["trade"] == schedules
    assert report["version"] == "synthetic-portfolio-competition-v1"
    with pytest.raises(FileExistsError):
        main()
