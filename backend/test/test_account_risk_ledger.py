from services.account_risk_ledger import summarize_account_risk


def _position(**updates):
    return {
        "position_id": "pos:1",
        "state": "open",
        "remaining_quantity": 100,
        "average_entry_price": 10.0,
        "hard_stop_price": 9.5,
        "active_stop_price": 9.5,
        **updates,
    }


def _stop(**updates):
    return {
        "status": "submitted",
        "broker_order_id": 77,
        "quantity": 100,
        "order_payload": {"stopPrice": 9.5},
        **updates,
    }


def test_summary_combines_today_realized_pnl_and_confirmed_open_stop_risk():
    result = summarize_account_risk(
        positions=[_position()],
        protective_intents={"pos:1": _stop()},
        sell_fills=[{
            "fill_key": "fill:1",
            "position_id": "pos:1",
            "quantity": 25,
            "price": 11.0,
        }],
    )

    assert result["daily_realized_pnl"] == 25.0
    assert result["aggregate_open_risk"] == 50.0
    assert result["risk_state_complete"] is True
    assert result["positions_at_risk"] == 1


def test_missing_or_mismatched_protection_fails_closed():
    result = summarize_account_risk(
        positions=[_position()],
        protective_intents={"pos:1": _stop(quantity=99)},
        sell_fills=[],
    )

    assert result["risk_state_complete"] is False
    assert result["aggregate_open_risk"] == 0
    assert result["incomplete_reasons"] == ["unverified_protection:pos:1"]


def test_empty_strategy_journal_is_authoritative_zero_risk():
    result = summarize_account_risk(
        positions=[], protective_intents={}, sell_fills=[]
    )

    assert result == {
        "daily_realized_pnl": 0.0,
        "aggregate_open_risk": 0.0,
        "risk_state_complete": True,
        "positions_at_risk": 0,
        "incomplete_reasons": [],
    }


def test_pending_entry_blocks_additional_risk_even_before_first_fill():
    result = summarize_account_risk(
        positions=[_position(state="entry_pending", remaining_quantity=0, average_entry_price=None)],
        protective_intents={},
        sell_fills=[],
    )

    assert result["risk_state_complete"] is False
    assert result["incomplete_reasons"] == ["entry_pending:pos:1"]


def test_unsettled_non_protective_order_blocks_new_account_risk():
    result = summarize_account_risk(
        positions=[_position()],
        protective_intents={},
        sell_fills=[],
        unsettled_intents=[{
            "idempotency_key": "exit:abcdefgh",
            "status": "unknown",
        }],
        require_broker_protection=False,
    )

    assert result["risk_state_complete"] is False
    assert result["incomplete_reasons"] == ["unsettled_order:exit:abcdefgh"]


def test_verified_stop_above_entry_counts_as_zero_capital_at_risk():
    result = summarize_account_risk(
        positions=[_position(active_stop_price=10.25)],
        protective_intents={"pos:1": _stop(order_payload={"stopPrice": 10.25})},
        sell_fills=[],
    )

    assert result["risk_state_complete"] is True
    assert result["aggregate_open_risk"] == 0


def test_local_stop_is_authoritative_without_broker_order():
    result = summarize_account_risk(
        positions=[_position()],
        protective_intents={},
        sell_fills=[],
        require_broker_protection=False,
    )

    assert result["risk_state_complete"] is True
    assert result["aggregate_open_risk"] == 50
    assert result["positions_at_risk"] == 1


def test_invalid_local_stop_fails_closed():
    result = summarize_account_risk(
        positions=[_position(active_stop_price=9.0)],
        protective_intents={},
        sell_fills=[],
        require_broker_protection=False,
    )

    assert result["risk_state_complete"] is False
    assert result["incomplete_reasons"] == ["unverified_protection:pos:1"]
