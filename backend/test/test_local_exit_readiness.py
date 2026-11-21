import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from services.local_exit_readiness import evaluate_local_exit_readiness


NOW = 1_700_000_000


def _healthy(**overrides):
    values = {
        "now": NOW,
        "account_stream": {
            "status": "connected",
            "ts": NOW - 2,
            "subscription_errors": [],
        },
        "exit_worker": {
            "status": "ok",
            "ts": NOW - 1,
            "stop_apply_enabled": True,
            "exit_submit_enabled": True,
            "kill_switch": False,
            "errors": 0,
        },
        "database_available": True,
        "trading_authorization": {
            "auto_trader_enabled": True,
            "has_trading_credentials": True,
            "has_refresh_token": True,
            "reauthorization_required": False,
            "seconds_to_reauthorization": 86_400,
        },
        "execute_trades": True,
        "broker_stops_enabled": False,
        "local_stop_apply_enabled": True,
        "exit_submit_enabled": True,
        "kill_switch": False,
    }
    values.update(overrides)
    return evaluate_local_exit_readiness(**values)


def test_healthy_local_exit_path_allows_entry_readiness():
    result = _healthy()

    assert result.ready is True
    assert result.reasons == ()
    assert result.account_stream_age_seconds == 2
    assert result.exit_worker_age_seconds == 1


def test_missing_transient_state_after_restart_fails_closed():
    result = _healthy(account_stream=None, exit_worker=None)

    assert result.ready is False
    assert "account_stream_missing" in result.reasons
    assert "exit_worker_missing" in result.reasons


def test_stale_or_degraded_stream_fails_closed():
    result = _healthy(
        account_stream={
            "status": "degraded",
            "ts": NOW - 20,
            "subscription_errors": [{"service": "NASDAQ_BOOK"}],
        }
    )

    assert result.ready is False
    assert "account_stream_not_connected" in result.reasons
    assert "account_stream_stale" in result.reasons
    assert "account_stream_subscription_error" in result.reasons


def test_stale_or_erroring_exit_worker_fails_closed():
    result = _healthy(
        exit_worker={
            "status": "ok",
            "ts": NOW - 5,
            "stop_apply_enabled": True,
            "exit_submit_enabled": True,
            "kill_switch": False,
            "errors": 1,
        }
    )

    assert result.ready is False
    assert "exit_worker_stale" in result.reasons
    assert "exit_worker_cycle_errors" in result.reasons


def test_database_and_authorization_are_mandatory():
    result = _healthy(
        database_available=False,
        trading_authorization={
            "auto_trader_enabled": True,
            "has_trading_credentials": True,
            "has_refresh_token": True,
            "reauthorization_required": False,
            "seconds_to_reauthorization": 300,
        },
    )

    assert result.ready is False
    assert "trading_journal_unavailable" in result.reasons
    assert "schwab_reauthorization_too_close" in result.reasons


def test_local_execution_switches_must_all_be_enabled():
    result = _healthy(
        execute_trades=False,
        local_stop_apply_enabled=False,
        exit_submit_enabled=False,
    )

    assert result.ready is False
    assert "execution_disabled" in result.reasons
    assert "local_stop_persistence_disabled" in result.reasons
    assert "local_exit_submission_disabled" in result.reasons


def test_required_backup_worker_must_be_independently_healthy():
    missing = _healthy(backup_required=True, backup_exit_worker=None)
    stale = _healthy(
        backup_required=True,
        backup_exit_worker={
            "status": "ok",
            "ts": NOW - 10,
            "stop_apply_enabled": True,
            "exit_submit_enabled": True,
            "kill_switch": False,
            "errors": 0,
        },
    )
    healthy = _healthy(
        backup_required=True,
        backup_exit_worker={
            "status": "ok",
            "ts": NOW - 1,
            "stop_apply_enabled": True,
            "exit_submit_enabled": True,
            "kill_switch": False,
            "errors": 0,
        },
    )

    assert "backup_exit_worker_missing" in missing.reasons
    assert "backup_exit_worker_stale" in stale.reasons
    assert healthy.ready is True
