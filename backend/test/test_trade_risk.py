from services.trade_risk import (
    ExitPolicy,
    RiskLimits,
    calculate_long_position_size,
    evaluate_long_exit,
)


def test_position_size_is_risk_based_and_includes_slippage():
    result = calculate_long_position_size(
        account_equity=100_000,
        buying_power=50_000,
        entry_price=10,
        stop_price=9.50,
        limits=RiskLimits(risk_per_trade_pct=0.005, slippage_bps=20),
    )

    assert result.allowed is True
    assert result.risk_budget == 500
    assert result.risk_per_share == 0.52
    assert result.quantity == 961
    assert result.estimated_loss_at_stop <= result.risk_budget


def test_position_size_respects_notional_and_liquidity_caps():
    result = calculate_long_position_size(
        account_equity=10_000,
        buying_power=10_000,
        entry_price=20,
        stop_price=19.90,
        average_minute_volume=2_000,
        limits=RiskLimits(
            risk_per_trade_pct=0.05,
            max_position_pct=0.10,
            max_volume_participation_pct=0.005,
            slippage_bps=0,
        ),
    )

    assert result.quantity == 10
    assert result.caps["position"] == 50
    assert result.caps["liquidity"] == 10


def test_position_size_fails_closed_at_daily_loss_limit():
    result = calculate_long_position_size(
        account_equity=20_000,
        buying_power=20_000,
        entry_price=5,
        stop_price=4.80,
        daily_realized_pnl=-400,
    )

    assert result.allowed is False
    assert result.reason == "daily_loss_limit_reached"


def test_position_size_fails_closed_at_aggregate_open_risk_limit():
    result = calculate_long_position_size(
        account_equity=20_000,
        buying_power=20_000,
        entry_price=5,
        stop_price=4.80,
        aggregate_open_risk=300,
    )

    assert result.allowed is False
    assert result.reason == "aggregate_open_risk_limit_reached"


def test_position_size_rejects_stop_at_or_above_entry():
    result = calculate_long_position_size(
        account_equity=20_000,
        buying_power=20_000,
        entry_price=5,
        stop_price=5,
    )

    assert result.allowed is False
    assert result.reason == "invalid_long_stop"


def test_exit_priority_kill_switch_precedes_profit_management():
    result = evaluate_long_exit(
        quantity=100,
        entry_price=10,
        hard_stop_price=9.50,
        current_price=12,
        peak_price=12,
        partial_taken=False,
        trading_disabled=True,
    )

    assert result.action == "exit_all"
    assert result.reason == "kill_switch"


def test_exit_takes_one_partial_at_configured_r_multiple():
    result = evaluate_long_exit(
        quantity=101,
        entry_price=10,
        hard_stop_price=9,
        current_price=11.5,
        peak_price=11.5,
        partial_taken=False,
    )

    assert result.action == "take_partial"
    assert result.quantity == 50
    assert result.active_stop == 10


def test_exit_runner_uses_peak_based_trailing_stop():
    result = evaluate_long_exit(
        quantity=50,
        entry_price=10,
        hard_stop_price=9,
        current_price=11.2,
        peak_price=12,
        partial_taken=True,
    )

    assert result.action == "exit_all"
    assert result.reason == "managed_stop"
    assert result.active_stop == 11.25


def test_exit_never_moves_an_existing_stop_backward():
    result = evaluate_long_exit(
        quantity=50,
        entry_price=10,
        hard_stop_price=9,
        active_stop_price=10.75,
        current_price=11,
        peak_price=11,
        partial_taken=True,
    )

    assert result.action == "hold"
    assert result.active_stop == 10.75


def test_exit_requires_price_and_order_flow_confirmation_for_reversal():
    common = dict(
        quantity=50,
        entry_price=10,
        hard_stop_price=9,
        current_price=10.5,
        peak_price=10.8,
        partial_taken=True,
        momentum_reversal=True,
    )

    assert evaluate_long_exit(**common, order_flow_imbalance=None).action == "hold"
    confirmed = evaluate_long_exit(**common, order_flow_imbalance=-0.40)
    assert confirmed.action == "exit_all"
    assert confirmed.reason == "confirmed_momentum_reversal"


def test_stale_quote_fails_closed_for_open_position():
    result = evaluate_long_exit(
        quantity=10,
        entry_price=10,
        hard_stop_price=9,
        current_price=10.2,
        peak_price=10.2,
        partial_taken=False,
        quote_is_stale=True,
    )

    assert result.action == "exit_all"
    assert result.reason == "market_data_stale"
