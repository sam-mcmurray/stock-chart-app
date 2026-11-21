from copy import deepcopy
from dataclasses import replace

import pytest

from drl_agent.portfolio_replay import PortfolioReplay
from drl_agent.simulator import Action
from drl_agent.watch_replay import WatchConfig
from test.test_drl_simulator import BASE, dataset

WATCH = WatchConfig(1, 20, 3, 20_000_000, 10)


def observations(symbol, **kwargs):
    result = dataset(**kwargs)
    result["manifest"]["symbol"] = symbol
    for state in result["states"]:
        state["symbol"] = symbol
        for kind, items in state["inputs"].items():
            for item in items:
                item["record"]["symbol"] = symbol
                if kind == "scanner":
                    p = item["record"]["payload"]
                    p["watch_inputs"] = {"version": "scanner-watch-inputs-v1", "observed_at": BASE,
                                         "price": 10, "rvol": 4, "float_shares": 1_000_000}
                    p["semantic_signal"]["hype"]["evidence_article_ids"] = [symbol + "-news"]
    return result


def test_shared_500_cash_allows_four_entries_and_exits():
    replay = PortfolioReplay([observations(s) for s in "ABCD"], watch_config=WATCH)
    assert replay.account()["cash"] == 500
    *_, event = replay.step({s: Action.ENTER for s in "DCBA"})
    assert set(event["account"]["positions"]) == set("ABCD")
    fills = [e["fill"] for e in event["symbols"].values() if e["fill"]]
    assert event["account"]["cash"] == pytest.approx(500 - sum(f["quantity"] * f["price"] + f["fee"] for f in fills))
    assert event["account"]["completed_round_trips"] == 0
    *_, exits = replay.step({s: Action.EXIT for s in "ABCD"})
    assert not exits["account"]["positions"]
    assert all(exits["symbols"][s]["fill"]["side"] == "sell" for s in "ABCD")
    assert exits["account"]["completed_round_trips"] == 4
    replay.step({})
    assert replay.account()["cash"] == pytest.approx(500 + replay.account()["realized_pnl"])


def test_aggregate_risk_is_reserved_across_symbols():
    replay = PortfolioReplay([observations(s) for s in "AB"], watch_config=WATCH)
    for sim in replay.simulators.values():
        sim.risk = replace(sim.risk, max_aggregate_open_risk_pct=0.002)
    *_, event = replay.step({s: Action.ENTER for s in "AB"})
    assert event["symbols"]["A"]["fill"]
    assert event["symbols"]["B"]["fill"] is None
    assert event["account"]["aggregate_open_risk"] <= 1


def test_watch_expiry_does_not_disable_protective_exit():
    replay = PortfolioReplay([observations("A", prices=(10, 10, 9))],
                             watch_config=replace(WATCH, watch_window_seconds=2))
    replay.step({"A": Action.ENTER})
    *_, event = replay.step({})
    assert event["symbols"]["A"]["reason"] == "hard_stop"
    assert event["symbols"]["A"]["fill"]["side"] == "sell"
    assert not event["account"]["positions"]


def test_exit_proceeds_not_reused_and_rewards_reconcile_once():
    replay = PortfolioReplay([observations("A")], watch_config=WATCH)
    replay.step({"A": Action.ENTER})
    buying_power = replay.account()["settled_buying_power"]
    replay.step({"A": Action.EXIT})
    assert replay.account()["settled_buying_power"] == buying_power
    assert replay.account()["unsettled_sale_proceeds"] > 0
    replay.step({})
    account = replay.account()
    assert sum(e["reward"] for e in replay.events) == pytest.approx(
        (account["equity"] - 500 - replay.config.drawdown_penalty * account["max_drawdown"]) / 500)


def test_prior_round_trips_do_not_block_entries_and_failures_do_not_count():
    replay = PortfolioReplay([observations("A", book=False)], watch_config=WATCH,
                             prior_round_trip_timestamps=[BASE - 604799, BASE - 20, BASE - 10])
    assert replay.account()["completed_round_trips"] == 3
    replay.step({"A": Action.ENTER})
    assert len(replay.round_trip_timestamps) == 3
    assert replay.account()["completed_round_trips"] == 3
    replay.reset()
    assert replay.account()["completed_round_trips"] == 3
    unrestricted = PortfolioReplay([observations("A")], watch_config=WATCH,
                                   prior_round_trip_timestamps=[BASE - 3, BASE - 2, BASE - 1])
    *_, event = unrestricted.step({"A": Action.ENTER})
    assert event["symbols"]["A"]["fill"]["side"] == "buy"
    assert unrestricted.report()["trade_limit"]["frequency_cap_enabled"] is False
    with pytest.raises(ValueError):
        PortfolioReplay([observations("A")], watch_config=WATCH, prior_round_trip_timestamps=[BASE + 1])


def test_unwatched_entries_blocked_and_open_end_not_liquidated():
    source = observations("A")
    replay = PortfolioReplay([source], watch_config=replace(WATCH, minimum_rvol=5))
    *_, event = replay.step({"A": Action.ENTER})
    assert event["symbols"]["A"]["reason"] == "outside_watch_targets"
    replay = PortfolioReplay([source], watch_config=WATCH)
    replay.step({"A": Action.ENTER})
    replay.step({})
    *_, end = replay.step({})
    assert end["truncated"] and end["open_at_end"]
    with pytest.raises(RuntimeError):
        replay.step({})


def test_order_reset_input_isolation_and_observation_visibility():
    sources = [observations(s) for s in "AB"]
    before = deepcopy(sources)
    replay = PortfolioReplay(sources, watch_config=WATCH)
    observation = replay.observation()
    observation["symbols"]["A"]["market"][0] = 999
    first = replay.step({"B": Action.ENTER, "A": Action.ENTER})
    replay.reset()
    assert first == replay.step({"A": Action.ENTER, "B": Action.ENTER})
    assert sources == before
    with pytest.raises(ValueError):
        replay.step({"A": True})
    with pytest.raises(ValueError):
        PortfolioReplay([sources[0], sources[0]], watch_config=WATCH)
    with pytest.raises(ValueError):
        PortfolioReplay([sources[0], observations("B", prices=(10, 10, 10))], watch_config=WATCH)


def test_stale_held_position_blocks_new_entry_but_retains_exit_request():
    replay = PortfolioReplay([observations("A", prices=(10,) * 6, stale_at=(2, 3, 4, 5)),
                              observations("B", prices=(10,) * 6)], watch_config=WATCH)
    replay.step({"A": Action.ENTER})
    for _ in range(3):
        replay.step({})
    *_, event = replay.step({"A": Action.EXIT, "B": Action.ENTER})
    assert event["symbols"]["B"]["reason"] == "stale_portfolio_valuation"
    assert event["account"]["positions"]["A"]["pending_sell"] > 0


def test_scale_in_and_partial_exit_are_one_round_trip():
    source = observations("A", prices=(10,) * 5)
    for state in source["states"]:
        for item in state["inputs"]["levelone_quote"]:
            item["record"]["payload"]["ask_size_shares"] = 4
    replay = PortfolioReplay([source], watch_config=WATCH,
                             prior_round_trip_timestamps=[BASE - 20, BASE - 10])
    replay.step({"A": Action.ENTER})
    assert replay.account()["positions"]["A"]["quantity"] == 1
    assert replay.account()["completed_round_trips"] == 2
    replay.step({"A": Action.ENTER})
    assert replay.account()["positions"]["A"]["quantity"] == 2
    assert len(replay.round_trip_timestamps) == 2
    replay.step({"A": Action.TAKE_PARTIAL})
    assert len(replay.round_trip_timestamps) == 2
    replay.step({"A": Action.EXIT})
    assert len(replay.round_trip_timestamps) == 3
    assert not replay.account()["positions"]


def test_unlimited_frequency_still_cannot_recycle_unsettled_proceeds():
    source = observations("A", prices=(10,) * 31)
    for state in source["states"]:
        for item in state["inputs"]["minute_candle"]:
            item["record"]["event_ts"] = BASE - 60
    replay = PortfolioReplay([source], watch_config=WATCH)
    while not replay.done:
        replay.step({"A": Action.EXIT if replay.account()["positions"] else Action.ENTER})
    assert replay.account()["completed_round_trips"] > 3
    assert replay.purchase_cost <= 500
    assert replay.account()["settled_buying_power"] < 10.01
    assert replay.account()["cash"] > 450
    assert replay.events[-1]["symbols"]["A"]["fill"] is None
