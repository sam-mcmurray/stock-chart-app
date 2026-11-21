from services.replay_engine import ReplayConfig, run_news_hype_replay
import pytest


START = 1_000
END = 2_000


def _signal(ts=1_050, score=0.8, **updates):
    row = {
        "symbol": "NVDA",
        "ts": ts,
        "price": 10.0,
        "hype": {
            "feature_version": "news-hype-v1",
            "as_of_ts": ts,
            "long_hype_score": score,
        },
    }
    row.update(updates)
    return row


def _bar(ts, open_price=10.0, high=10.2, low=9.9, close=10.1, volume=100_000, **updates):
    row = {
        "event_ts": ts,
        "received_ts": ts + 60,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }
    row.update(updates)
    return row


def _quote(ts, bid=10.0, ask=10.02, **updates):
    row = {
        "observation_type": "levelone_quote",
        "event_ts": ts,
        "received_ts": ts + 1,
        "bid": bid,
        "ask": ask,
        "payload": {},
    }
    row.update(updates)
    return row


def _book(ts, bid_depth=600, ask_depth=400, **updates):
    row = {
        "observation_type": "level2_book",
        "event_ts": ts,
        "received_ts": ts + 1,
        "payload": {
            "service": "NASDAQ_BOOK",
            "bids": [{"price": 10.0, "size": bid_depth}],
            "asks": [{"price": 10.02, "size": ask_depth}],
        },
    }
    row.update(updates)
    return row


def _config(**updates):
    values = {
        "assumed_spread_bps": 0,
        "entry_slippage_bps": 0,
        "exit_slippage_bps": 0,
        "entry_latency_seconds": 2,
        "maximum_entry_wait_seconds": 180,
        "entry_volume_participation_pct": 1.0,
        "exit_volume_participation_pct": 1.0,
    }
    values.update(updates)
    return ReplayConfig(**values)


def test_replay_enters_only_on_a_later_bar_and_never_calls_broker():
    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal()],
        market_observations=[
            _bar(1_040),
            _bar(1_100),
            _bar(1_160, low=9.7, close=9.8),
        ],
        start_ts=START,
        end_ts=END,
        config=_config(),
    )

    assert result["broker_calls"] == 0
    assert result["ranking_only"] is True
    assert result["trades"][0]["entry_event_ts"] == 1_100
    assert result["trades"][0]["exit_fills"][0]["reason"] == "stop"


def test_replay_uses_observed_quote_spread_and_attaches_book_evidence():
    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal()],
        market_observations=[
            _bar(1_100),
            _quote(1_104),
            _book(1_103),
            _bar(1_160, low=9.7, close=9.8),
        ],
        start_ts=START,
        end_ts=END,
        config=_config(),
    )

    trade = result["trades"][0]
    assert result["replay_version"] == "news-hype-replay-v2"
    assert result["audit"]["input_levelone_quotes"] == 1
    assert result["audit"]["input_level2_books"] == 1
    assert trade["entry_price_source"] == "levelone_quote"
    assert abs(trade["entry_price"] - 10.02) < 1e-9
    assert trade["entry_book_service"] == "NASDAQ_BOOK"
    assert trade["entry_bid_depth"] == 600
    assert trade["entry_ask_depth"] == 400
    assert trade["entry_book_imbalance"] == 0.2


def test_replay_blocks_entry_when_received_quote_reports_halt():
    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal()],
        market_observations=[
            _bar(1_100),
            _quote(1_104, payload={"security_status": "HALTED"}),
        ],
        start_ts=START,
        end_ts=END,
        config=_config(),
    )

    assert result["audit"]["halted_entries"] == 1
    assert result["metrics"]["trade_count"] == 0


def test_replay_rejects_future_features_future_prices_and_late_revisions():
    future_feature = _signal()
    future_feature["hype"]["as_of_ts"] = future_feature["ts"] + 1
    result = run_news_hype_replay(
        "NVDA",
        signals=[future_feature],
        market_observations=[
            _bar(1_100),
            _bar(1_100, close=99, high=99, low=9, received_ts=1_300),
            _bar(2_100),
        ],
        start_ts=START,
        end_ts=END,
        config=_config(maximum_data_delay_seconds=120),
    )

    assert result["audit"]["excluded_future_signal_features"] == 1
    assert result["audit"]["excluded_late_revision"] == 1
    assert result["audit"]["excluded_future_market"] == 1
    assert result["metrics"]["trade_count"] == 0


def test_replay_never_treats_retrospective_imports_as_live_bars():
    historical = _bar(1_100, received_ts=1_101)
    historical["observation_type"] = "historical_minute_candle"

    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal()],
        market_observations=[historical],
        start_ts=START,
        end_ts=END,
        config=_config(),
    )

    assert result["audit"]["input_market_observations"] == 1
    assert result["metrics"]["trade_count"] == 0


def test_stop_is_assumed_before_target_when_both_touch_in_one_bar():
    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal()],
        market_observations=[
            _bar(1_100),
            _bar(1_160, high=10.5, low=9.7, close=10.2),
        ],
        start_ts=START,
        end_ts=END,
        config=_config(),
    )

    trade = result["trades"][0]
    assert trade["exit_fills"][0]["reason"] == "stop"
    assert trade["total_pnl"] < 0


def test_gap_through_stop_fills_at_worse_open_with_exit_slippage():
    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal()],
        market_observations=[
            _bar(1_100),
            _bar(1_160, open_price=9.0, high=9.2, low=8.8, close=9.1),
        ],
        start_ts=START,
        end_ts=END,
        config=_config(exit_slippage_bps=100),
    )

    trade = result["trades"][0]
    assert trade["gap_exit"] is True
    assert trade["exit_fills"][0]["price"] == 8.91


def test_volume_caps_create_partial_entry_and_multi_bar_stop_exit():
    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal(quantity=100)],
        market_observations=[
            _bar(1_100, volume=40),
            _bar(1_160, open_price=9.7, high=9.8, low=9.6, close=9.7, volume=10),
            _bar(1_220, open_price=9.6, high=9.7, low=9.5, close=9.6, volume=50),
        ],
        start_ts=START,
        end_ts=END,
        config=_config(
            entry_volume_participation_pct=0.5,
            exit_volume_participation_pct=0.5,
        ),
    )

    trade = result["trades"][0]
    assert trade["requested_quantity"] == 100
    assert trade["filled_quantity"] == 20
    assert trade["partial_entry"] is True
    assert trade["partial_exit"] is True
    assert [fill["quantity"] for fill in trade["exit_fills"]] == [5, 15]
    assert trade["status"] == "closed"


def test_halted_entry_is_blocked_and_acceptance_needs_enough_trades():
    result = run_news_hype_replay(
        "NVDA",
        signals=[_signal()],
        market_observations=[_bar(1_100, halted=True)],
        start_ts=START,
        end_ts=END,
        config=_config(),
    )

    assert result["audit"]["halted_entries"] == 1
    assert result["metrics"]["trade_count"] == 0
    assert result["acceptance"]["eligible"] is False
    assert result["acceptance"]["passed"] is False


def test_invalid_replay_configuration_fails_explicitly():
    with pytest.raises(ValueError, match="between zero and one"):
        run_news_hype_replay(
            "NVDA",
            signals=[],
            market_observations=[],
            start_ts=START,
            end_ts=END,
            config=ReplayConfig(risk_per_trade_pct=2),
        )
