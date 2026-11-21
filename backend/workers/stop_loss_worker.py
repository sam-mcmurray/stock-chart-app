import json
import logging
import os
import time

import redis

from routers.auth import get_trading_access_token
from drl_agent.risk import evaluate_stop_loss
from schwab.accounts import SchwabAccountClient
from schwab.orders import SchwabOrderClient
from schwab.user_preferences import UserPreferences

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)

H_STOP_LOSS_HEARTBEAT = os.getenv("STOP_LOSS_HEARTBEAT_KEY", "agent:stop_loss:heartbeat")
K_STOP_LOSS_EVENTS = os.getenv("STOP_LOSS_EVENTS_KEY", "agent:stop_loss:events")
H_STOP_LOSS_PEAKS = os.getenv("STOP_LOSS_PEAKS_KEY", "agent:stop_loss:peaks")


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _position_snapshot(position: dict) -> dict:
    instrument = position.get("instrument") or {}
    qty = _num(position.get("longQuantity"))
    avg = _num(position.get("averageLongPrice"), default=position.get("averagePrice") or 0.0)
    market_value = _num(position.get("marketValue"))
    current = market_value / qty if qty > 0 and market_value > 0 else _num(position.get("marketPrice"))
    return {
        "symbol": instrument.get("symbol") or instrument.get("description") or "UNKNOWN",
        "quantity": qty,
        "average_price": avg,
        "current_price": current,
    }


class StopLossWorker:
    def __init__(self):
        self._stop = False
        self.interval_seconds = int(os.getenv("AGENT_STOP_LOSS_INTERVAL_SECONDS", "5"))
        self.max_loss_pct = float(os.getenv("AGENT_MAX_LOSS_PCT", "0.03"))
        self.trailing_loss_pct = float(os.getenv("AGENT_TRAILING_LOSS_PCT", str(self.max_loss_pct)))
        max_loss_amount = os.getenv("AGENT_MAX_LOSS_DOLLARS")
        trailing_loss_amount = os.getenv("AGENT_TRAILING_LOSS_DOLLARS")
        self.max_loss_amount = float(max_loss_amount) if max_loss_amount else None
        self.trailing_loss_amount = float(trailing_loss_amount) if trailing_loss_amount else None
        self.execute_trades = os.getenv("AGENT_EXECUTE_TRADES", "false").lower() == "true"
        self.monitor_enabled = os.getenv("AGENT_LEGACY_STOP_LOSS_ENABLED", "false").lower() == "true"

    def stop(self):
        self._stop = True

    def _heartbeat(self, status: str, **extra):
        try:
            r.set(H_STOP_LOSS_HEARTBEAT, json.dumps({"status": status, "ts": int(time.time()), **extra}))
        except Exception:
            pass

    def _peak_key(self, account_id: str, symbol: str) -> str:
        return f"{account_id}:{symbol.upper()}"

    def _update_peak_price(self, account_id: str, symbol: str, average_price: float, current_price: float) -> float:
        key = self._peak_key(account_id, symbol)
        try:
            existing = _num(r.hget(H_STOP_LOSS_PEAKS, key))
            peak = max(existing, average_price, current_price)
            r.hset(H_STOP_LOSS_PEAKS, key, str(peak))
            return peak
        except Exception:
            return max(average_price, current_price)

    def _clear_peak_price(self, account_id: str, symbol: str):
        try:
            r.hdel(H_STOP_LOSS_PEAKS, self._peak_key(account_id, symbol))
        except Exception:
            pass

    def _log_event(self, payload: dict):
        try:
            r.lpush(K_STOP_LOSS_EVENTS, json.dumps(payload))
            r.ltrim(K_STOP_LOSS_EVENTS, 0, int(os.getenv("STOP_LOSS_EVENTS_MAX_LEN", "200")) - 1)
        except Exception:
            pass

    def _sell_position(self, order_client: SchwabOrderClient, account_id: str, symbol: str, quantity: int):
        order_payload = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL",
                    "quantity": quantity,
                    "instrument": {"symbol": symbol, "assetType": "EQUITY"},
                }
            ],
        }
        if not self.execute_trades:
            return {"dry_run": True, "order_payload": order_payload}
        return order_client.place_order(account_id=account_id, order_payload=order_payload)

    def run_once(self) -> dict:
        if not self.monitor_enabled:
            return {
                "checked": 0,
                "triggered": 0,
                "execute_trades": self.execute_trades,
                "monitor": "disabled",
            }

        try:
            access_token = get_trading_access_token()
        except RuntimeError as exc:
            return {
                "checked": 0,
                "triggered": 0,
                "execute_trades": self.execute_trades,
                "auto_trader": "off",
                "detail": str(exc),
            }

        prefs = UserPreferences(access_token)
        account_client = SchwabAccountClient(user_prefs=prefs, access_token=access_token)
        order_client = SchwabOrderClient(prefs, access_token=access_token)
        accounts = account_client.get_all_accounts(include_positions=True) or []
        checked = 0
        triggered = 0

        for account in accounts:
            sa = (account or {}).get("securitiesAccount") or {}
            account_id = sa.get("accountNumber")
            for raw_position in sa.get("positions") or []:
                position = _position_snapshot(raw_position)
                if position["quantity"] <= 0:
                    self._clear_peak_price(account_id, position["symbol"])
                    continue
                checked += 1
                peak_price = self._update_peak_price(
                    account_id,
                    position["symbol"],
                    position["average_price"],
                    position["current_price"],
                )
                decision = evaluate_stop_loss(
                    quantity=position["quantity"],
                    average_price=position["average_price"],
                    current_price=position["current_price"],
                    max_loss_pct=self.max_loss_pct,
                    max_loss_amount=self.max_loss_amount,
                    peak_price=peak_price,
                    trailing_loss_pct=self.trailing_loss_pct,
                    trailing_loss_amount=self.trailing_loss_amount,
                )
                if not decision.triggered:
                    continue

                triggered += 1
                event = {
                    "symbol": position["symbol"],
                    "account_id": account_id,
                    "quantity": int(position["quantity"]),
                    "position": position,
                    "stop_loss": decision.to_dict(),
                    "ts": int(time.time()),
                    "status": "pending",
                }
                try:
                    event["order_response"] = self._sell_position(
                        order_client,
                        account_id,
                        position["symbol"],
                        int(position["quantity"]),
                    )
                    event["status"] = "submitted" if self.execute_trades else "dry_run"
                except Exception as exc:
                    event["status"] = "error"
                    event["error"] = str(exc)
                    logger.exception("stop loss sell failed")
                self._log_event(event)

        return {
            "checked": checked,
            "triggered": triggered,
            "execute_trades": self.execute_trades,
            "auto_trader": "on",
        }

    def run_forever(self):
        while not self._stop:
            try:
                result = self.run_once()
                self._heartbeat("ok", **result)
            except Exception as exc:
                logger.exception("stop loss worker cycle failed")
                self._heartbeat("error", error=str(exc))
            finally:
                time.sleep(self.interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    StopLossWorker().run_forever()
