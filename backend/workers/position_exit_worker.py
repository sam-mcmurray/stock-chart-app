from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import redis

from routers.auth import get_trading_access_token
from schwab.market_data import SchwabMarketDataClient
from schwab.orders import SchwabOrderClient
from schwab.user_preferences import UserPreferences
from services.exit_order_coordinator import synchronize_exit
from services.entry_market_gate import normalize_schwab_equity_quote
from services.market_stream_store import load_market_stream_snapshot
from services.position_exit_controller import (
    ExitStreamLimits,
    evaluate_position_exit,
)
from services.trade_journal import (
    list_positions_for_protection,
    update_position_risk_state,
)
from services.trade_risk import ExitPolicy


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
HEARTBEAT_KEY = os.getenv("AGENT_EXIT_HEARTBEAT_KEY", "agent:exit_controller:heartbeat")
EVENTS_KEY = os.getenv("AGENT_EXIT_EVENTS_KEY", "agent:exit_controller:events")
EVENT_SIGNATURES_KEY = os.getenv(
    "AGENT_EXIT_EVENT_SIGNATURES_KEY", "agent:exit_controller:event_signatures"
)
LEVELONE_KEY = os.getenv("SCHWAB_LEVELONE_HASH_KEY", "schwab:stream:levelone")
NASDAQ_BOOK_KEY = os.getenv("SCHWAB_NASDAQ_BOOK_HASH_KEY", "schwab:stream:nasdaq_book")
NYSE_BOOK_KEY = os.getenv("SCHWAB_NYSE_BOOK_HASH_KEY", "schwab:stream:nyse_book")
CHART_KEY_PREFIX = os.getenv("SCHWAB_CHART_KEY_PREFIX", "schwab:stream:chart:")
r = redis.from_url(REDIS_URL, decode_responses=True)


def _float_env(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


class PositionExitWorker:
    def __init__(self) -> None:
        self.interval_seconds = max(
            0.25, _float_env("AGENT_EXIT_CONTROLLER_INTERVAL_SECONDS", 1.0)
        )
        self.instance = os.getenv("AGENT_EXIT_WORKER_INSTANCE", "primary")
        self.rest_timeout_seconds = max(
            0.5, _float_env("AGENT_EXIT_REST_TIMEOUT_SECONDS", 2.0)
        )
        self.kill_switch = os.getenv(
            "AGENT_TRADING_KILL_SWITCH", "false"
        ).lower() == "true"
        self.apply_stops = os.getenv(
            "AGENT_EXIT_APPLY_STOPS", "false"
        ).lower() == "true"
        self.submit_exits = bool(
            os.getenv("AGENT_EXECUTE_TRADES", "false").lower() == "true"
            and os.getenv("AGENT_EXIT_SUBMIT_ENABLED", "false").lower() == "true"
        )
        self.policy = ExitPolicy(
            partial_profit_r=_float_env("AGENT_PARTIAL_PROFIT_R", 1.5),
            partial_fraction=_float_env("AGENT_PARTIAL_FRACTION", 0.5),
            break_even_r=_float_env("AGENT_BREAK_EVEN_R", 1.0),
            runner_activation_r=_float_env("AGENT_RUNNER_ACTIVATION_R", 2.0),
            runner_trail_r=_float_env("AGENT_RUNNER_TRAIL_R", 0.75),
            order_flow_exit_threshold=_float_env(
                "AGENT_ORDER_FLOW_EXIT_THRESHOLD", -0.35
            ),
        )
        self.stream_limits = ExitStreamLimits(
            max_quote_age_seconds=_float_env("AGENT_MAX_QUOTE_AGE_SECONDS", 3.0),
            max_receive_age_seconds=_float_env("AGENT_MAX_QUOTE_AGE_SECONDS", 3.0),
            max_book_age_seconds=_float_env("AGENT_MAX_LEVEL2_AGE_SECONDS", 2.0),
            max_book_receive_age_seconds=_float_env(
                "AGENT_MAX_LEVEL2_RECEIVE_AGE_SECONDS", 2.0
            ),
        )

    def _heartbeat(self, status: str, **extra: Any) -> None:
        try:
            r.set(
                HEARTBEAT_KEY,
                json.dumps(
                    {
                        "status": status,
                        "ts": int(time.time()),
                        "instance": self.instance,
                        **extra,
                    }
                ),
            )
        except Exception:
            pass

    def _snapshot(self, symbol: str) -> dict[str, Any]:
        return load_market_stream_snapshot(
            r,
            symbol=symbol,
            levelone_key=LEVELONE_KEY,
            nasdaq_book_key=NASDAQ_BOOK_KEY,
            nyse_book_key=NYSE_BOOK_KEY,
            chart_key_prefix=CHART_KEY_PREFIX,
            chart_limit=10,
        )

    def _rest_snapshot(
        self, symbol: str, market_client: SchwabMarketDataClient
    ) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        quote = normalize_schwab_equity_quote(
            market_client.get_quote(symbol),
            symbol=symbol,
            fetched_at_ms=now_ms,
        )
        market_ts = max(
            int(quote.get("trade_time_ms") or 0),
            int(quote.get("quote_time_ms") or 0),
        )
        quote_age = max(0.0, (now_ms - market_ts) / 1000) if market_ts else None
        return {
            "levelone": {
                "symbol": quote["symbol"],
                "bid": quote["bid"],
                "ask": quote["ask"],
                "last": quote["last"],
                "mark": quote["mark"],
                "security_status": quote["security_status"],
                "delayed": quote.get("realtime") is not True,
            },
            "quote_age_seconds": quote_age,
            "levelone_receive_age_seconds": 0.0,
            "book_metrics": {},
            "candles": [],
        }

    def _record_actionable(self, position_id: str, evaluation: dict[str, Any]) -> None:
        signature = json.dumps(
            {
                "action": evaluation.get("action"),
                "quantity": evaluation.get("quantity"),
                "reason": evaluation.get("reason"),
                "active_stop": evaluation.get("active_stop"),
            },
            sort_keys=True,
        )
        try:
            previous = r.hget(EVENT_SIGNATURES_KEY, position_id)
            if previous == signature:
                return
            r.hset(EVENT_SIGNATURES_KEY, position_id, signature)
            r.lpush(
                EVENTS_KEY,
                json.dumps(
                    {"position_id": position_id, "ts": int(time.time()), **evaluation}
                ),
            )
            r.ltrim(EVENTS_KEY, 0, 199)
        except Exception:
            pass

    def run_once(self) -> dict[str, Any]:
        counts = {
            "checked": 0,
            "hold": 0,
            "take_partial": 0,
            "exit_all": 0,
            "blocked": 0,
            "stop_ratchets": 0,
            "rest_fallbacks": 0,
            "rest_fallback_errors": 0,
            "exit_coordination_actions": {},
            "errors": 0,
        }
        order_client = None
        market_client = None
        for context in list_positions_for_protection():
            position = context["position"]
            if str(position.get("state") or "").lower() not in {
                "entry_pending",
                "open",
                "partial_taken",
                "runner",
            }:
                continue
            if float(position.get("remaining_quantity") or 0) < 1:
                continue
            counts["checked"] += 1
            try:
                snapshot_error = None
                try:
                    snapshot = self._snapshot(position["symbol"])
                except Exception as exc:
                    snapshot = {}
                    snapshot_error = f"{type(exc).__name__}: {exc}"
                evaluation_obj = evaluate_position_exit(
                    position,
                    snapshot,
                    trading_disabled=self.kill_switch,
                    policy=self.policy,
                    limits=self.stream_limits,
                )
                data_source = "stream"
                rest_error = None
                if evaluation_obj.quote_is_stale is True:
                    try:
                        if market_client is None:
                            token = get_trading_access_token()
                            preferences = UserPreferences(token)
                            market_client = SchwabMarketDataClient(
                                preferences,
                                access_token=token,
                                timeout_seconds=self.rest_timeout_seconds,
                            )
                        rest_snapshot = self._rest_snapshot(
                            position["symbol"], market_client
                        )
                        evaluation_obj = evaluate_position_exit(
                            position,
                            rest_snapshot,
                            trading_disabled=self.kill_switch,
                            policy=self.policy,
                            limits=self.stream_limits,
                        )
                        data_source = "schwab_rest"
                        counts["rest_fallbacks"] += 1
                    except Exception as exc:
                        rest_error = f"{type(exc).__name__}: {exc}"
                        counts["rest_fallback_errors"] += 1
                evaluation = evaluation_obj.to_dict()
                evaluation["market_data_source"] = data_source
                if snapshot_error:
                    evaluation["stream_snapshot_error"] = snapshot_error
                if rest_error:
                    evaluation["rest_fallback_error"] = rest_error
                result = {"updated": False, "reason": "shadow_mode"}
                if self.apply_stops:
                    result = update_position_risk_state(
                        position_id=position["position_id"],
                        expected_remaining_quantity=position["remaining_quantity"],
                        peak_price=evaluation["peak_price"],
                        active_stop_price=evaluation["active_stop"],
                        decision=evaluation,
                        event_ts=int(time.time() * 1000),
                    )
                action = evaluation["action"]
                counts[action if action in counts else "blocked"] += 1
                if result.get("updated"):
                    counts["stop_ratchets"] += 1
                if self.submit_exits and evaluation["action"] in {"exit_all", "take_partial"}:
                    if order_client is None:
                        token = get_trading_access_token()
                        preferences = UserPreferences(token)
                        order_client = SchwabOrderClient(
                            preferences, access_token=token
                        )
                coordination = synchronize_exit(
                    position=position,
                    exit_decision=evaluation,
                    protective_intent=context.get("protective_intent"),
                    exit_intent=context.get("exit_intent"),
                    order_client=order_client,
                    execute=self.submit_exits,
                )
                coordination_action = coordination["decision"]["action"]
                counts["exit_coordination_actions"][coordination_action] = (
                    counts["exit_coordination_actions"].get(coordination_action, 0) + 1
                )
                evaluation["exit_coordination"] = coordination
                if action != "hold":
                    self._record_actionable(position["position_id"], evaluation)
            except Exception:
                counts["errors"] += 1
                logger.exception(
                    "position exit evaluation failed for %s",
                    position.get("position_id"),
                )
        return {
            **counts,
            "kill_switch": self.kill_switch,
            "execution_mode": "enabled" if self.submit_exits else "shadow",
            "stop_apply_enabled": self.apply_stops,
            "exit_submit_enabled": self.submit_exits,
            "worker_instance": self.instance,
        }

    def run_forever(self) -> None:
        while True:
            try:
                self._heartbeat("ok", **self.run_once())
            except Exception as exc:
                logger.exception("position exit controller cycle failed")
                self._heartbeat(
                    "error",
                    error=str(exc),
                    kill_switch=self.kill_switch,
                    execution_mode="enabled" if self.submit_exits else "shadow",
                    stop_apply_enabled=self.apply_stops,
                    exit_submit_enabled=self.submit_exits,
                )
            time.sleep(self.interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    PositionExitWorker().run_forever()
