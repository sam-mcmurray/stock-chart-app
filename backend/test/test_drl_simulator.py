from datetime import datetime, timezone

import pytest

from drl_agent.dataset import build_dataset
from drl_agent.simulator import Action, SimulationConfig, TradingSimulator

BASE = int(datetime(2026, 9, 22, 14, tzinfo=timezone.utc).timestamp())

def dataset(prices=(10, 10, 10.1, 10.2), *, stale_at=(), bid_size=10000, book=True):
    rows = []
    scanner = [{"id": "scanner", "symbol": "TEST", "source": "scanner_shadow", "ts": BASE,
                "created_at": datetime.fromtimestamp(BASE, timezone.utc),
                "payload": {"semantic_signal": {"hype": {"as_of_ts": BASE, "long_hype_score": 0.8}}}}]
    for i, bid in enumerate(prices):
        t = BASE + i
        event = t - 10 if i in stale_at else t
        quote = {"bid": bid, "ask": bid + 0.01, "bid_size_shares": bid_size,
                 "ask_size_shares": 10000, "quote_time_ms": event * 1000,
                 "bid_time_ms": event * 1000, "ask_time_ms": event * 1000,
                 "received_at_ms": t * 1000, "delayed": False, "security_status": "Normal",
                 "regular_market_quote": True, "exchange_id": "Q"}
        rows.append({"observation_key": f"q{i}", "symbol": "TEST", "source": "schwab_stream",
                     "event_ts": event, "received_ts": t, "observation_type": "levelone_quote", "payload": quote})
        if book:
            rows.append({"observation_key": f"b{i}", "symbol": "TEST", "source": "schwab_stream",
                         "event_ts": t, "received_ts": t, "observation_type": "level2_book",
                         "payload": {"service": "NASDAQ_BOOK", "received_at_ms": t * 1000,
                                     "market_time_ms": t * 1000,
                                     "bids": [{"price": bid, "size": 10000}],
                                     "asks": [{"price": bid + 0.01, "size": 10000}]}})
    rows.append({"observation_key": "c1", "symbol": "TEST", "source": "schwab_stream",
                 "event_ts": BASE - 100, "received_ts": BASE - 40, "observation_type": "minute_candle",
                 "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100000,
                 "payload": {"received_at_ms": (BASE - 40) * 1000}})
    return build_dataset("TEST", start_ts=BASE, end_ts=BASE + len(prices) - 1,
                         step_seconds=1, scanner_rows=scanner, market_rows=rows)


def test_buy_partial_exit_reconcile_cash_fees_and_rewards():
    sim = TradingSimulator(dataset())
    _, reward1, _, _, entry = sim.step(Action.ENTER)
    assert entry["fill"]["side"] == "buy"
    qty = sim.quantity
    assert 0 < qty < 1000
    buy = entry["fill"]
    assert sim.cash == pytest.approx(10000 - qty * buy["price"] - buy["fee"])
    assert reward1 < 0
    _, reward2, _, _, partial = sim.step(Action.TAKE_PARTIAL)
    assert partial["fill"]["quantity"] == qty // 2
    assert sim.partial_taken
    _, reward3, terminated, truncated, exit_event = sim.step(Action.EXIT)
    assert sim.quantity == 0 and sim.pending_sell == 0
    assert not terminated and truncated
    fills = [e["fill"] for e in sim.events]
    expected = 10000 - qty * buy["price"] - buy["fee"] + sum(
        f["quantity"] * f["price"] - f["fee"] for f in fills[1:])
    assert sim.cash == pytest.approx(expected)
    assert sim.realized == pytest.approx(expected - 10000)
    assert sum((reward1, reward2, reward3)) == pytest.approx(
        (sim.equity - 10000 - sim.config.drawdown_penalty * sim.max_drawdown) / 10000)
    with pytest.raises(RuntimeError):
        sim.step(Action.HOLD)


@pytest.mark.parametrize("kwargs", [{"stale_at": (1,)}, {"book": False}])
def test_entry_requires_fresh_quote_and_book(kwargs):
    sim = TradingSimulator(dataset(**kwargs))
    *_, event = sim.step(Action.ENTER)
    assert event["fill"] is None
    assert sim.cash == 10000 and sim.quantity == 0


def test_gap_stop_overrides_hold_and_fills_at_adverse_bid():
    sim = TradingSimulator(dataset((10, 10, 9)))
    sim.step(Action.ENTER)
    *_, event = sim.step(Action.HOLD)
    assert event["reason"] == "hard_stop"
    assert event["fill"]["side"] == "sell"
    assert event["fill"]["price"] < 9
    assert sim.realized < 0 and sim.quantity == 0


def test_partial_liquidity_leaves_exit_pending_and_no_fake_end_liquidation():
    sim = TradingSimulator(dataset(bid_size=4))
    sim.step(Action.ENTER)
    qty = sim.quantity
    *_, first = sim.step(Action.EXIT)
    assert first["fill"]["quantity"] == 1
    assert sim.pending_sell == qty - 1
    *_, final = sim.step(Action.HOLD)
    assert final["fill"]["quantity"] == 1
    assert final["truncated"] and final["open_at_end"]
    assert sim.quantity == qty - 2


def test_stale_exit_never_fills_and_retains_exposure():
    sim = TradingSimulator(dataset((10, 10, 8), stale_at=(2,)))
    sim.step(Action.ENTER)
    qty = sim.quantity
    *_, event = sim.step(Action.EXIT)
    assert event["fill"] is None
    assert sim.pending_sell == sim.quantity == qty
    assert event["open_at_end"]


def test_partial_action_cannot_repeatedly_sell_runner():
    sim = TradingSimulator(dataset())
    sim.step(Action.ENTER)
    sim.step(Action.TAKE_PARTIAL)
    remaining = sim.quantity
    *_, event = sim.step(Action.TAKE_PARTIAL)
    assert event["fill"] is None and sim.quantity == remaining


def test_deterministic_reset_and_no_input_mutation():
    source = dataset()
    sim = TradingSimulator(source)
    first = sim.step(Action.ENTER)
    sim.reset()
    assert sim.step(Action.ENTER) == first
    assert source["manifest"]["training_ready"] is False


def test_rejects_invalid_config_and_daily_loss_blocks_entry():
    with pytest.raises(ValueError):
        TradingSimulator(dataset(), config=SimulationConfig(fee_bps=float("nan")))
    sim = TradingSimulator(dataset())
    sim.realized = -500
    *_, event = sim.step(Action.ENTER)
    assert event["reason"] == "daily_loss_limit_reached"
    assert event["fill"] is None


def test_afterhours_quote_flag_cannot_allow_entry(monkeypatch):
    monkeypatch.setitem(dataset.__globals__, "BASE", BASE + 12 * 3600)
    sim = TradingSimulator(dataset())
    *_, event = sim.step(Action.ENTER)
    assert event["reason"] == "outside_regular_session"
    assert sim.quantity == 0
