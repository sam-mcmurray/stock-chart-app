import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from drl_agent.risk import evaluate_stop_loss


def test_stop_loss_triggers_at_max_loss_pct():
    decision = evaluate_stop_loss(
        quantity=10,
        average_price=100,
        current_price=97,
        max_loss_pct=0.03,
    )

    assert decision.triggered is True
    assert decision.reason == "stop_loss(30.00>=30.00)"
    assert decision.trigger_type == "fixed"
    assert decision.stop_price == 97


def test_stop_loss_uses_explicit_dollar_cap():
    decision = evaluate_stop_loss(
        quantity=10,
        average_price=100,
        current_price=98,
        max_loss_pct=0.10,
        max_loss_amount=20,
    )

    assert decision.triggered is True
    assert decision.max_loss_amount == 20
    assert decision.loss_pct == 0.02


def test_stop_loss_ignores_empty_position():
    decision = evaluate_stop_loss(
        quantity=0,
        average_price=100,
        current_price=90,
        max_loss_pct=0.03,
    )

    assert decision.triggered is False
    assert decision.loss_amount == 0


def test_stop_loss_ignores_zero_current_price():
    decision = evaluate_stop_loss(
        quantity=10,
        average_price=100,
        current_price=0,
        max_loss_pct=0.03,
    )

    assert decision.triggered is False
    assert decision.loss_amount == 0
    assert decision.stop_price == 97


def test_stop_loss_sanitizes_non_finite_values():
    decision = evaluate_stop_loss(
        quantity=float("nan"),
        average_price=float("inf"),
        current_price=90,
        max_loss_pct=float("nan"),
        max_loss_amount=float("inf"),
        peak_price=float("nan"),
        trailing_loss_pct=float("-inf"),
        trailing_loss_amount=float("inf"),
    )

    assert decision.triggered is False
    assert decision.loss_amount == 0
    assert decision.loss_pct == 0
    assert decision.max_loss_amount == 0
    assert decision.stop_price == 0
    assert decision.peak_price == 90


def test_trailing_stop_triggers_from_peak_price():
    decision = evaluate_stop_loss(
        quantity=10,
        average_price=100,
        current_price=106,
        max_loss_pct=0.03,
        peak_price=110,
        trailing_loss_pct=0.03,
    )

    assert decision.triggered is True
    assert decision.trigger_type == "trailing"
    assert decision.reason == "trailing_stop(106.00<=106.70)"
    assert decision.peak_price == 110
    assert decision.trailing_stop_price == 106.7
    assert decision.loss_amount == 0


def test_trailing_stop_does_not_trigger_before_position_has_moved_up():
    decision = evaluate_stop_loss(
        quantity=10,
        average_price=100,
        current_price=97,
        max_loss_pct=0.05,
        peak_price=100,
        trailing_loss_pct=0.03,
    )

    assert decision.triggered is False
    assert decision.trailing_stop_price == 97
