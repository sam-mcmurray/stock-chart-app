from unittest.mock import MagicMock

from workers import shadow_trade_worker


def _candidate(ts=1_000):
    return {
        "id": "candidate-1",
        "symbol": "NVDA",
        "ts": ts,
        "payload": {
            "shadow_stage": "actionable",
            "semantic_signal": {
                "hype": {"feature_version": "news-hype-v1", "long_hype_score": 0.8}
            },
            "trade_plan": {"stop": 9.8},
        },
    }


def _snapshot(*, stale=False):
    return {
        "levelone": {
            "symbol": "NVDA", "bid": 10.0, "ask": 10.02, "last": 10.01,
            "ask_size_shares": 1000, "security_status": "Normal", "delayed": False,
            "quote_time_ms": 1_100_000, "trade_time_ms": 1_100_000,
            "received_at_ms": 1_101_000,
        },
        "levelone_receive_age_seconds": 10.0 if stale else 0.1,
        "quote_age_seconds": 10.0 if stale else 0.1,
        "book_metrics": {
            "service": "NASDAQ_BOOK", "receive_age_seconds": 0.1,
            "market_age_seconds": 0.1, "bid_depth": 6000, "ask_depth": 4000,
            "imbalance": 0.2,
        },
        "candles": [
            {"open": 10, "high": 10.1, "low": 9.9, "close": 10, "volume": 100_000}
        ],
    }


def test_shadow_candidate_opens_without_broker_path(monkeypatch):
    worker = shadow_trade_worker.ShadowTradeWorker()
    monkeypatch.setattr(shadow_trade_worker, "list_active_positions", MagicMock(return_value=[]))
    monkeypatch.setattr(
        shadow_trade_worker, "list_pending_opportunities", MagicMock(return_value=[_candidate()])
    )
    monkeypatch.setattr(worker, "_snapshot", MagicMock(return_value=_snapshot()))
    opened = MagicMock(return_value={"position_id": "shadow:1"})
    monkeypatch.setattr(shadow_trade_worker, "open_position", opened)

    counts = {"candidates_checked": 0, "candidates_rejected": 0,
              "candidates_skipped": 0, "positions_opened": 0}
    worker._evaluate_candidates(1_110, counts)

    assert counts["positions_opened"] == 1
    assert opened.call_args.kwargs["entry_price"] > 10.02
    assert opened.call_args.kwargs["quantity"] <= 250
    assert opened.call_args.kwargs["payload"]["broker_calls"] == 0


def test_stale_candidate_quote_remains_monitored_without_open(monkeypatch):
    worker = shadow_trade_worker.ShadowTradeWorker()
    monkeypatch.setattr(shadow_trade_worker, "list_active_positions", MagicMock(return_value=[]))
    monkeypatch.setattr(
        shadow_trade_worker, "list_pending_opportunities", MagicMock(return_value=[_candidate()])
    )
    monkeypatch.setattr(worker, "_snapshot", MagicMock(return_value=_snapshot(stale=True)))
    reject = MagicMock(return_value=True)
    monkeypatch.setattr(shadow_trade_worker, "record_candidate_disposition", reject)
    opened = MagicMock()
    monkeypatch.setattr(shadow_trade_worker, "open_position", opened)

    counts = {"candidates_checked": 0, "candidates_rejected": 0,
              "candidates_monitoring": 0,
              "candidates_skipped": 0, "positions_opened": 0}
    worker._evaluate_candidates(1_110, counts)

    assert counts["candidates_rejected"] == 0
    assert counts["candidates_monitoring"] == 1
    assert reject.call_args.kwargs["status"] == "monitoring"
    assert reject.call_args.kwargs["reason"] == "entry_quote_invalid_or_stale"
    opened.assert_not_called()


def test_monitored_candidate_can_enter_when_market_data_becomes_fresh(monkeypatch):
    worker = shadow_trade_worker.ShadowTradeWorker()
    candidate = _candidate()
    monkeypatch.setattr(shadow_trade_worker, "list_active_positions", MagicMock(return_value=[]))
    monkeypatch.setattr(
        shadow_trade_worker, "list_pending_opportunities", MagicMock(return_value=[candidate])
    )
    monkeypatch.setattr(
        worker, "_snapshot", MagicMock(side_effect=[_snapshot(stale=True), _snapshot()])
    )
    disposition = MagicMock(return_value=True)
    monkeypatch.setattr(shadow_trade_worker, "record_candidate_disposition", disposition)
    opened = MagicMock(return_value={"position_id": "shadow:1"})
    monkeypatch.setattr(shadow_trade_worker, "open_position", opened)

    first = {"candidates_checked": 0, "candidates_rejected": 0,
             "candidates_monitoring": 0, "candidates_skipped": 0,
             "positions_opened": 0}
    second = dict(first)
    worker._evaluate_candidates(1_110, first)
    worker._evaluate_candidates(1_111, second)

    assert first["candidates_monitoring"] == 1
    assert second["positions_opened"] == 1
    assert disposition.call_args.kwargs["status"] == "monitoring"
    opened.assert_called_once()


def test_stale_position_data_never_creates_shadow_fill(monkeypatch):
    worker = shadow_trade_worker.ShadowTradeWorker()
    position = {
        "position_id": "shadow:1", "symbol": "NVDA", "state": "open",
        "remaining_quantity": 10, "entry_price": 10.0, "hard_stop_price": 9.8,
        "active_stop_price": 9.8, "peak_price": 10.0, "partial_taken": False,
    }
    monkeypatch.setattr(
        shadow_trade_worker, "list_active_positions", MagicMock(return_value=[position])
    )
    monkeypatch.setattr(worker, "_snapshot", MagicMock(return_value=_snapshot(stale=True)))
    apply = MagicMock()
    monkeypatch.setattr(shadow_trade_worker, "apply_evaluation", apply)
    counts = {"positions_checked": 0, "hold": 0, "take_partial": 0,
              "exit_all": 0, "blocked": 0, "stale_holds": 0}

    worker._evaluate_positions(1_110, counts)

    assert counts["stale_holds"] == 1
    apply.assert_not_called()


def test_closed_session_does_no_lifecycle_work(monkeypatch):
    worker = shadow_trade_worker.ShadowTradeWorker()
    monkeypatch.setattr(
        shadow_trade_worker, "market_session",
        MagicMock(return_value={"state": "closed", "is_regular": False}),
    )
    monkeypatch.setattr(shadow_trade_worker, "summary", MagicMock(return_value={
        "positions": {}, "candidates": {}, "execution_mode": "shadow",
        "broker_calls": 0, "live_activation_eligible": False,
    }))
    candidates = MagicMock()
    positions = MagicMock()
    monkeypatch.setattr(worker, "_evaluate_candidates", candidates)
    monkeypatch.setattr(worker, "_evaluate_positions", positions)
    expire = MagicMock(return_value=4)
    monkeypatch.setattr(shadow_trade_worker, "expire_stale_opportunities", expire)

    result = worker.run_once(now=1_000)

    assert result["market_session"] == "closed"
    assert result["broker_calls"] == 0
    assert result["opportunities_expired"] == 4
    expire.assert_called_once_with(expire_before_ts=1_000 - worker.candidate_max_age)
    candidates.assert_not_called()
    positions.assert_not_called()
