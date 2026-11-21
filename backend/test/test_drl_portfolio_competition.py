from datetime import datetime, timedelta

import pytest

from drl_agent.portfolio_competition import EnterExitPolicy, compare_portfolio_sessions
from test.test_drl_portfolio_replay import observations, WATCH


def shift(value, seconds):
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if isinstance(v, (int, float)) and k in {"ts", "decision_ts", "event_ts", "received_ts", "as_of_ts", "observed_at"}:
                result[k] = v + seconds
            elif isinstance(v, (int, float)) and k.endswith("_ms"):
                result[k] = v + seconds * 1000
            else:
                result[k] = shift(v, seconds)
        return result
    if isinstance(value, list):
        return [shift(v, seconds) for v in value]
    if isinstance(value, datetime):
        return value + timedelta(seconds=seconds)
    return value


def test_daily_weekly_competition_carries_cash_and_round_trip_history():
    day = [observations("A", prices=(10, 10, 10.1))]
    sessions = [day, shift(day, 86400), shift(day, 2 * 86400), shift(day, 3 * 86400)]
    report = compare_portfolio_sessions(sessions, {"a": EnterExitPolicy, "b": EnterExitPolicy}, watch_config=WATCH)
    a = report["competitors"]["a"]
    assert a == report["competitors"]["b"]
    assert a["complete"]
    assert [d["round_trips"] for d in a["daily"]] == [1, 1, 1, 1]
    assert a["daily"][1]["starting_equity"] == a["daily"][0]["ending_equity"]
    assert a["weekly"][0]["sessions"] == 4
    assert a["weekly"][0]["round_trips"] == 4
    assert report["maximum_round_trips_per_rolling_week"] is None
    assert report["frequency_cap_enabled"] is False
    assert a["weekly"][0]["net_pnl"] == pytest.approx(sum(d["net_pnl"] for d in a["daily"]))
    assert report["competitors"]["stay-out"]["weekly"][0]["net_pnl"] == 0
    assert not report["promotion_allowed"] and not report["training_started"]


def test_unresolved_position_prevents_fabricated_next_day_reset():
    day = [observations("A", prices=(10, 10))]
    report = compare_portfolio_sessions([day, shift(day, 86400)],
                                       {"a": EnterExitPolicy, "b": EnterExitPolicy}, watch_config=WATCH)
    assert not report["competitors"]["a"]["complete"]
    assert len(report["competitors"]["a"]["daily"]) == 1
    with pytest.raises(ValueError):
        compare_portfolio_sessions([day, day], {"a": EnterExitPolicy, "b": EnterExitPolicy}, watch_config=WATCH)


@pytest.mark.parametrize("exit_price", [10.1, 9.9])
def test_each_new_week_resets_to_500_not_profit_or_loss(exit_price):
    day = [observations("A", prices=(10, 10, exit_price))]
    sessions = [shift(day, days * 86400) for days in (0, 1, 7, 8)]
    report = compare_portfolio_sessions(sessions, {"a": EnterExitPolicy, "b": EnterExitPolicy}, watch_config=WATCH)
    result = report["competitors"]["a"]
    daily = result["daily"]
    assert daily[1]["starting_equity"] == daily[0]["ending_equity"]
    assert daily[2]["starting_equity"] == 500
    assert daily[2]["weekly_capital_reset"]
    assert daily[2]["capital_adjustment"] == pytest.approx(500 - daily[1]["ending_equity"])
    assert daily[3]["starting_equity"] == daily[2]["ending_equity"]
    assert not daily[3]["weekly_capital_reset"]
    for week in result["weekly"]:
        assert week["starting_equity"] == 500
        assert week["net_pnl"] == pytest.approx(week["ending_equity"] - 500)
    assert result["max_drawdown"] == max(w["max_drawdown"] for w in result["weekly"])
