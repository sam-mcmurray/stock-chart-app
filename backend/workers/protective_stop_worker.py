from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import redis

from routers.auth import get_trading_access_token
from schwab.orders import SchwabOrderClient
from schwab.user_preferences import UserPreferences
from services.protective_stop_manager import synchronize_protective_stop
from services.trade_journal import list_positions_for_protection


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
HEARTBEAT_KEY = os.getenv("PROTECTIVE_STOP_HEARTBEAT_KEY", "agent:protective_stop:heartbeat")
INTERVAL_SECONDS = max(1, int(os.getenv("AGENT_PROTECTIVE_STOP_INTERVAL_SECONDS", "2")))
r = redis.from_url(REDIS_URL, decode_responses=True)


class ProtectiveStopWorker:
    def __init__(self) -> None:
        self.broker_stops_enabled = os.getenv(
            "AGENT_BROKER_PROTECTIVE_STOPS_ENABLED", "false"
        ).lower() == "true"
        self.execute = bool(
            self.broker_stops_enabled
            and os.getenv("AGENT_EXECUTE_TRADES", "false").lower() == "true"
        )

    def _heartbeat(self, status: str, **extra: Any) -> None:
        try:
            r.set(HEARTBEAT_KEY, json.dumps({"status": status, "ts": int(time.time()), **extra}))
        except Exception:
            pass

    def run_once(self) -> dict[str, Any]:
        if not self.broker_stops_enabled:
            return {
                "checked": 0,
                "create": 0,
                "replace": 0,
                "cancel": 0,
                "blocked": 0,
                "errors": 0,
                "execute_trades": False,
                "broker_stops_enabled": False,
                "protection_mode": "local_trigger",
            }
        contexts = list_positions_for_protection()
        order_client = None
        if self.execute and contexts:
            token = get_trading_access_token()
            order_client = SchwabOrderClient(UserPreferences(token), access_token=token)
        counts = {
            "checked": 0,
            "create": 0,
            "replace": 0,
            "cancel": 0,
            "blocked": 0,
            "errors": 0,
        }
        for context in contexts:
            counts["checked"] += 1
            try:
                result = synchronize_protective_stop(
                    position=context["position"],
                    protective_intent=context["protective_intent"],
                    exit_intent=context.get("exit_intent"),
                    order_client=order_client,
                    execute=self.execute,
                )
            except Exception:
                counts["errors"] += 1
                logger.exception(
                    "protective stop reconciliation failed for position %s",
                    context["position"].get("position_id"),
                )
                continue
            action = result["decision"]["action"]
            if action in counts:
                counts[action] += 1
        return {
            **counts,
            "execute_trades": self.execute,
            "broker_stops_enabled": self.broker_stops_enabled,
            "protection_mode": "broker_held_stop",
        }

    def run_forever(self) -> None:
        while True:
            try:
                self._heartbeat("ok", **self.run_once())
            except Exception as exc:
                logger.exception("protective stop worker cycle failed")
                self._heartbeat("error", error=str(exc), execute_trades=self.execute)
            time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    ProtectiveStopWorker().run_forever()
