from copy import deepcopy
from dataclasses import replace

import pytest

from drl_agent.synthetic_portfolio import SyntheticPortfolio
from test.test_synthetic_execution import fixture, resign

BUY = dict(side="buy", quantity=100, limit_price=11, stop_price=10)
SELL = dict(side="sell", quantity=100, limit_price=1)


def portfolio(symbols="AB", **kwargs):
    return SyntheticPortfolio([fixture(symbol=s, **kwargs) for s in symbols], session_date="2018-07-16")


def test_combined_cash_fees_risk_and_round_trips():
    sim = portfolio("ABCD")
    event = sim.step({s: BUY for s in "DCBA"})
    fills = [t["fill"] for t in event["symbols"].values() if t["fill"]]
    assert sim.account()["cash"] == pytest.approx(500 - sum(f["quantity"] * f["price"] + f["fee"] for f in fills))
    assert sim.account()["aggregate_open_risk"] <= 500 * .015
    assert len(fills) >= 2
    buying_power = sim.account()["settled_buying_power"]
    sim.step({s: SELL for s in "ABCD"})
    account = sim.account()
    assert not account["positions"]
    assert account["completed_round_trips"] == len(fills)
    assert account["cash"] == pytest.approx(500 + account["realized_pnl"])
    assert account["settled_buying_power"] == pytest.approx(buying_power)
    assert account["unsettled_sale_proceeds"] > 0


def test_cash_cannot_be_spent_twice_even_with_large_risk_limits():
    sim = portfolio()
    for child in sim.simulators.values():
        child.risk = replace(child.risk, max_position_pct=1, risk_per_trade_pct=1,
                             max_aggregate_open_risk_pct=1, daily_loss_limit_pct=1)
    event = sim.step({"B": BUY, "A": BUY})
    assert event["symbols"]["A"]["fill"]["quantity"] == 47
    assert event["symbols"]["B"]["fill"] is None
    assert 0 <= sim.account()["cash"] < 11
    sim.step({"A": SELL, "B": BUY})
    assert not sim.account()["positions"]
    assert sim.events[-1]["symbols"]["B"]["fill"] is None


def test_aggregate_risk_and_daily_loss_apply_across_symbols():
    sim = portfolio()
    for child in sim.simulators.values():
        child.risk = replace(child.risk, max_aggregate_open_risk_pct=.002)
    event = sim.step({"A": BUY, "B": BUY})
    assert event["symbols"]["A"]["fill"]["quantity"] == 1
    assert event["symbols"]["B"]["fill"] is None
    assert sim.account()["aggregate_open_risk"] < 1
    sim = portfolio()
    sim.simulators["A"].realized = -11
    event = sim.step({"B": BUY})
    assert event["symbols"]["B"]["reason"] == "daily_loss_limit_reached"


def test_unpriced_position_blocks_buys_but_keeps_protective_exit():
    datasets = [fixture(symbol=s) for s in "AB"]
    for book in datasets[0]["synthetic_depth"]["snapshots"][2:]:
        book["bids"][0]["size"] = 0
    resign(datasets[0]["synthetic_depth"], "scenario_sha256")
    resign(datasets[0], "join_sha256")
    sim = SyntheticPortfolio(datasets, session_date="2018-07-16")
    sim.step({"A": BUY})
    event = sim.step({"B": BUY})
    assert event["symbols"]["B"]["reason"] == "unpriced_portfolio"
    assert event["account"]["equity"] is None
    event = sim.step({"B": BUY})
    assert event["symbols"]["A"]["exit_signal"] == "market_data_unavailable"
    assert event["symbols"]["A"]["pending_exit"] > 0
    assert sim.report()["valuation_incomplete"]


def test_scale_ins_partial_exits_single_round_trip_and_priority():
    sim = portfolio()
    one = {**BUY, "quantity": 1}
    sim.step({"A": one})
    sim.step({"A": one})
    sim.step({"A": {**SELL, "quantity": 1}})
    assert sim.account()["completed_round_trips"] == 0
    event = sim.step({"B": BUY, "A": SELL})
    assert list(event["symbols"]) == ["A", "B"]
    assert sim.account()["completed_round_trips"] == 1


def test_protective_exit_precedes_other_symbols_and_overrides_add():
    datasets = [fixture(symbol=s) for s in "AZ"]
    for book in datasets[1]["synthetic_depth"]["snapshots"][2:]:
        book["bids"][0]["price"] = 9
        book["asks"][0]["price"] = 9.01
    resign(datasets[1]["synthetic_depth"], "scenario_sha256")
    resign(datasets[1], "join_sha256")
    sim = SyntheticPortfolio(datasets, session_date="2018-07-16")
    sim.step({"Z": BUY})
    sim.step({})
    event = sim.step({"A": BUY, "Z": BUY})
    assert list(event["symbols"]) == ["Z", "A"]
    assert event["symbols"]["Z"]["fill"]["side"] == "sell"
    assert event["symbols"]["Z"]["exit_signal"] == "hard_stop"
    assert "Z" not in event["account"]["positions"]


def test_input_isolation_determinism_and_all_orders_validated_before_advance():
    datasets = [fixture(symbol=s) for s in "AB"]
    before = deepcopy(datasets)
    sim = SyntheticPortfolio(datasets, session_date="2018-07-16")
    obs = sim.observation()
    obs["symbols"]["A"]["book"]["bids"][0]["price"] = -1
    with pytest.raises(ValueError):
        sim.step({"A": BUY, "B": {"side": "bad"}})
    assert sim.index == 0 and all(s.index == 0 for s in sim.simulators.values())
    event = sim.step({"B": BUY, "A": BUY})
    other = SyntheticPortfolio(datasets[::-1], session_date="2018-07-16")
    assert event == other.step({"A": BUY, "B": BUY})
    assert datasets == before
    with pytest.raises(ValueError):
        sim.step({"UNKNOWN": BUY})


def test_missing_grid_rejected_and_unfinished_positions_not_liquidated():
    with pytest.raises(ValueError, match="grids"):
        SyntheticPortfolio([fixture(symbol="A"), fixture(symbol="B", gap=True)], session_date="2018-07-16")
    with pytest.raises(ValueError):
        SyntheticPortfolio([fixture(), fixture()], session_date="2018-07-16")
    sim = portfolio()
    sim.step({"A": BUY})
    while not sim.done:
        sim.step({})
    assert sim.report()["account"]["positions"]
    assert sim.events[-1]["open_at_end"]
    assert sim.report()["broker_calls"] == 0 and not sim.report()["training_ready"]
    with pytest.raises(RuntimeError):
        sim.step({})
