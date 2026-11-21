from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LocalExitReadiness:
    ready: bool
    reasons: tuple[str, ...]
    account_stream_age_seconds: int | None
    exit_worker_age_seconds: int | None
    backup_exit_worker_age_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "reasons": list(self.reasons),
            "account_stream_age_seconds": self.account_stream_age_seconds,
            "exit_worker_age_seconds": self.exit_worker_age_seconds,
            "backup_exit_worker_age_seconds": self.backup_exit_worker_age_seconds,
        }


def _age(now: int, heartbeat: dict | None) -> int | None:
    try:
        ts = int((heartbeat or {}).get("ts") or 0)
    except (TypeError, ValueError):
        return None
    return max(0, now - ts) if ts > 0 else None


def evaluate_local_exit_readiness(
    *,
    now: int,
    account_stream: dict | None,
    exit_worker: dict | None,
    backup_exit_worker: dict | None = None,
    backup_required: bool = False,
    database_available: bool,
    trading_authorization: dict | None,
    execute_trades: bool,
    broker_stops_enabled: bool,
    local_stop_apply_enabled: bool,
    exit_submit_enabled: bool,
    kill_switch: bool,
    max_stream_age_seconds: int = 10,
    max_exit_worker_age_seconds: int = 3,
    minimum_reauthorization_seconds: int = 600,
) -> LocalExitReadiness:
    """Fail-closed readiness policy for entries protected only by local triggers."""
    reasons: list[str] = []
    stream_age = _age(now, account_stream)
    exit_age = _age(now, exit_worker)
    backup_exit_age = _age(now, backup_exit_worker)
    auth = trading_authorization or {}

    if not execute_trades:
        reasons.append("execution_disabled")
    if broker_stops_enabled:
        reasons.append("local_only_policy_not_active")
    if not local_stop_apply_enabled:
        reasons.append("local_stop_persistence_disabled")
    if not exit_submit_enabled:
        reasons.append("local_exit_submission_disabled")
    if kill_switch:
        reasons.append("trading_kill_switch_active")
    if not database_available:
        reasons.append("trading_journal_unavailable")

    if not account_stream:
        reasons.append("account_stream_missing")
    else:
        if account_stream.get("status") not in {"connected", "ok"}:
            reasons.append("account_stream_not_connected")
        if stream_age is None or stream_age > max_stream_age_seconds:
            reasons.append("account_stream_stale")
        if account_stream.get("subscription_errors"):
            reasons.append("account_stream_subscription_error")

    if not exit_worker:
        reasons.append("exit_worker_missing")
    else:
        if exit_worker.get("status") != "ok":
            reasons.append("exit_worker_unhealthy")
        if exit_age is None or exit_age > max_exit_worker_age_seconds:
            reasons.append("exit_worker_stale")
        if not exit_worker.get("stop_apply_enabled"):
            reasons.append("exit_worker_stop_persistence_inactive")
        if not exit_worker.get("exit_submit_enabled"):
            reasons.append("exit_worker_submission_inactive")
        if exit_worker.get("kill_switch"):
            reasons.append("exit_worker_kill_switch_active")
        if int(exit_worker.get("errors") or 0) > 0:
            reasons.append("exit_worker_cycle_errors")

    if backup_required:
        if not backup_exit_worker:
            reasons.append("backup_exit_worker_missing")
        else:
            if backup_exit_worker.get("status") != "ok":
                reasons.append("backup_exit_worker_unhealthy")
            if (
                backup_exit_age is None
                or backup_exit_age > max_exit_worker_age_seconds
            ):
                reasons.append("backup_exit_worker_stale")
            if not backup_exit_worker.get("stop_apply_enabled"):
                reasons.append("backup_exit_worker_stop_persistence_inactive")
            if not backup_exit_worker.get("exit_submit_enabled"):
                reasons.append("backup_exit_worker_submission_inactive")
            if backup_exit_worker.get("kill_switch"):
                reasons.append("backup_exit_worker_kill_switch_active")
            if int(backup_exit_worker.get("errors") or 0) > 0:
                reasons.append("backup_exit_worker_cycle_errors")

    if not auth.get("auto_trader_enabled"):
        reasons.append("auto_trader_disabled")
    if not auth.get("has_trading_credentials"):
        reasons.append("trading_credentials_missing")
    if not auth.get("has_refresh_token"):
        reasons.append("refresh_token_missing")
    if auth.get("reauthorization_required"):
        reasons.append("schwab_reauthorization_required")
    elif int(auth.get("seconds_to_reauthorization") or 0) < minimum_reauthorization_seconds:
        reasons.append("schwab_reauthorization_too_close")

    return LocalExitReadiness(
        ready=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        account_stream_age_seconds=stream_age,
        exit_worker_age_seconds=exit_age,
        backup_exit_worker_age_seconds=backup_exit_age,
    )
