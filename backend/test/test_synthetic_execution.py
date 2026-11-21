from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

from drl_agent.dataset import encode_dataset
from drl_agent.hf_replay_export import build_export
from drl_agent.historical_news import build_universe
from drl_agent.historical_news_join import join_news
from drl_agent.synthetic_depth import DepthConfig
from drl_agent.synthetic_execution import SyntheticExecution


def resign(data, key):
    data.pop(key, None)
    data[key] = hashlib.sha256(encode_dataset(data).encode()).hexdigest()


def fixture(*, bid_size=1000, ask_size=1000, gap=False, closing=False, day="2018-07-16", symbol="ABC"):
    times = pd.date_range(day + " " + ("15:57" if closing else "09:30"), periods=3 if closing else 8, freq="min")
    if gap:
        times = times.delete(1)
    n = len(times)
    frame = pd.DataFrame({"datetime": times, "Open": [10.] * n, "High": [11.] * n,
                          "Low": [9.] * n, "Close": [10.5] * n, "Volume": [100000] * n,
                          "source": ["pitrading"] * n})
    prices = build_export(frame, {"provider": "HF Data Library", "version": "raw",
                         "symbol": symbol, "sha256": "archive"},
                         start=day + "T13:30Z", end=day + "T20:00Z", depth_config=DepthConfig())
    for book in prices["synthetic_depth"]["snapshots"]:
        book["bids"] = [{"price": 10.49, "size": bid_size}]
        book["asks"] = [{"price": 10.5, "size": ask_size}]
    resign(prices["synthetic_depth"], "scenario_sha256")
    resign(prices, "export_sha256")
    start = int(pd.Timestamp(day + "T00:00Z").timestamp())
    news = build_universe([], start_ts=start, end_ts=start + 86400, enrichment_cutoff_ts=start + 86401)
    return join_news(prices, news)


def engine(**kwargs):
    return SyntheticExecution(fixture(**kwargs), session_date="2018-07-16")


def test_next_state_buy_sell_reconciles_costs_and_settlement():
    sim = engine()
    buy = sim.step(side="buy", quantity=100, limit_price=11, stop_price=10)
    assert buy["fill"]["quantity"] == 4
    assert buy["execution_ts"] == buy["decision_ts"] + 60
    assert buy["fill"]["source_candle_id"] == sim.states[1]["candle_id"]
    cost = 4 * buy["fill"]["price"] + buy["fill"]["fee"]
    assert sim.cash == pytest.approx(500 - cost)
    sell = sim.step(side="sell", quantity=100, limit_price=9)
    proceeds = 4 * sell["fill"]["price"] - sell["fill"]["fee"]
    assert sim.cash == pytest.approx(500 - cost + proceeds)
    assert sim.realized == pytest.approx(proceeds - cost)
    assert sell["settled_buying_power"] == pytest.approx(500 - cost)
    assert sim.quantity == 0


def test_partial_sell_and_unfilled_ioc_remainder_not_retried():
    sim = engine(bid_size=4, ask_size=8)
    buy = sim.step(side="buy", quantity=10, limit_price=11, stop_price=10)
    assert buy["fill"]["quantity"] == 2 and buy["unfilled_quantity"] == 8
    assert sim.step()["fill"] is None
    sell = sim.step(side="sell", quantity=10, limit_price=9)
    assert sell["fill"]["quantity"] == 1
    assert sim.quantity == 1
    while not sim.done:
        sim.step()
    assert sim.report()["open_at_end"]
    assert sim.quantity == 1
    with pytest.raises(RuntimeError):
        sim.step()


@pytest.mark.parametrize("kwargs,limit,reason", [({}, 10.5, "limit_not_marketable"),
    ({"ask_size": 0}, 11, "book_empty"), ({"gap": True}, 11, "missing_next_minute_or_latency")])
def test_missing_or_unmarketable_execution_does_not_fill(kwargs, limit, reason):
    sim = engine(**kwargs)
    result = sim.step(side="buy", quantity=1, limit_price=limit, stop_price=10)
    assert result["fill"] is None and result["reason"] == reason
    assert sim.cash == 500 and sim.quantity == 0


def test_close_boundary_no_shorting_and_risk_limited_scale_in():
    sim = engine(closing=True)
    sim.step(side="buy", quantity=1, limit_price=11, stop_price=10)
    result = sim.step(side="sell", quantity=10, limit_price=9)
    assert result["reason"] == "outside_regular_session" and sim.quantity == 1
    sim = engine()
    assert sim.step(side="sell", quantity=10, limit_price=9)["fill"] is None
    sim.step(side="buy", quantity=1, limit_price=11, stop_price=10)
    original_stop = sim.hard_stop
    assert sim.step(side="buy", quantity=1, limit_price=11, stop_price=9)["fill"]["quantity"] == 1
    assert sim.quantity == 2 and sim.hard_stop == original_stop


def repriced(bids, *, bid_size=1000, empty_asks_at=()):
    data = fixture(bid_size=bid_size)
    for index, price in bids.items():
        book = data["synthetic_depth"]["snapshots"][index]
        book["bids"][0]["price"] = price
        book["asks"][0]["price"] = price + 0.01
    for index in empty_asks_at:
        data["synthetic_depth"]["snapshots"][index]["asks"][0]["size"] = 0
    resign(data["synthetic_depth"], "scenario_sha256")
    resign(data, "join_sha256")
    return SyntheticExecution(data, session_date="2018-07-16")


def test_stop_observed_now_executes_later_and_persists_across_partial_fills():
    sim = repriced({i: 9 for i in range(2, 8)}, bid_size=4, empty_asks_at=(3, 4))
    sim.step(side="buy", quantity=2, limit_price=11, stop_price=10)
    assert sim.step()["fill"] is None
    exit1 = sim.step(side="buy", quantity=1, limit_price=11, stop_price=10)
    assert exit1["proposed_side"] == "buy" and exit1["side"] == "sell"
    assert exit1["exit_signal"] == "hard_stop"
    assert exit1["fill"]["price"] < 9 and exit1["pending_exit"] == 1
    assert sim.completed_round_trips == 0
    exit2 = sim.step()
    assert exit2["fill"]["quantity"] == 1 and sim.pending_exit == 0
    assert sim.quantity == 0 and sim.completed_round_trips == 1


def test_automatic_partial_profit_retries_only_remaining_target_once():
    sim = repriced({i: 11.4 for i in range(2, 8)}, bid_size=4)
    sim.step(side="buy", quantity=4, limit_price=11, stop_price=10)
    sim.step()
    first = sim.step()
    assert first["exit_signal"] == "partial_profit_target"
    assert first["fill"]["quantity"] == 1 and sim.pending_exit == 1
    assert sim.partial_taken and sim.active_stop >= sim.entry
    sim.step()
    assert sim.quantity == 2 and sim.pending_exit == 0
    assert sim.step()["fill"] is None


def test_runner_stop_ratchets_and_exit_uses_later_bid():
    sim = repriced({2: 11.7, 3: 11.6, 4: 11.2, 5: 10.8, 6: 10.7, 7: 10.6})
    sim.step(side="buy", quantity=2, limit_price=11, stop_price=10)
    sim.step()
    sim.step()
    stop = sim.active_stop
    assert stop > sim.entry
    sim.step()
    assert sim.active_stop >= stop
    exit_event = sim.step()
    assert exit_event["exit_signal"] == "managed_stop"
    assert exit_event["fill"]["price"] < 10.8
    assert sim.completed_round_trips == 1


def test_adds_share_original_risk_cap_and_no_averaging_down():
    sim = engine()
    sim.step(side="buy", quantity=1, limit_price=11, stop_price=10)
    sim.step(side="buy", quantity=100, limit_price=11, stop_price=10.49)
    assert sim.quantity == 4 and sim.hard_stop == 10
    assert sim.step(side="buy", quantity=100, limit_price=11, stop_price=10.49)["fill"] is None
    sim = repriced({2: 10.3})
    sim.step(side="buy", quantity=1, limit_price=11, stop_price=10)
    blocked = sim.step(side="buy", quantity=1, limit_price=11, stop_price=10)
    assert blocked["reason"] == "scale_in_below_entry_or_active_stop"
    assert sim.quantity == 1


def test_source_integrity_input_isolation_and_bad_order_no_clock_advance():
    data = fixture()
    before = deepcopy(data)
    sim = SyntheticExecution(data, session_date="2018-07-16")
    with pytest.raises(ValueError):
        sim.step(side="buy", quantity=True, limit_price=11, stop_price=10)
    assert sim.index == 0
    sim.step()
    assert data == before
    data["synthetic_depth"]["snapshots"][0]["synthetic"] = False
    with pytest.raises(ValueError, match="digest"):
        SyntheticExecution(data, session_date="2018-07-16")


def test_cli_explicit_orders_and_no_overwrite(tmp_path, monkeypatch):
    from drl_agent.synthetic_execution import main
    source, orders, output = (tmp_path / name for name in ("joined.json", "orders.json", "result.json"))
    source.write_text(encode_dataset(fixture()))
    orders.write_text(json.dumps([
        {"decision_index": 0, "side": "buy", "quantity": 1, "limit_price": 11, "stop_price": 10},
        {"decision_index": 1, "side": "sell", "quantity": 1, "limit_price": 9}]))
    monkeypatch.setattr("sys.argv", ["synthetic_execution", "--joined", str(source), "--orders", str(orders),
                                     "--session", "2018-07-16", "--output", str(output)])
    main()
    report = json.loads(output.read_text())
    assert report["finished"] and report["quantity"] == 0
    assert sum(e["fill"] is not None for e in report["events"]) == 2
    assert report["synthetic"] and report["broker_calls"] == 0
    with pytest.raises(FileExistsError):
        main()
