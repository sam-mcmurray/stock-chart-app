from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import redis

from services.decision_engine import market_session
from services.market_stream_store import load_market_stream_snapshot
from services.position_exit_controller import ExitStreamLimits, evaluate_position_exit
from services.shadow_trade_store import (
    apply_evaluation,
    ensure_schema,
    expire_stale_opportunities,
    list_active_positions,
    list_pending_opportunities,
    open_position,
    record_candidate_disposition,
    record_opportunity_disposition,
    summary,
)
from services.trade_risk import ExitPolicy, RiskLimits, calculate_long_position_size


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
HEARTBEAT_KEY = os.getenv("SHADOW_TRADE_HEARTBEAT_KEY", "agent:shadow_trade:heartbeat")
LEVELONE_KEY = os.getenv("SCHWAB_LEVELONE_HASH_KEY", "schwab:stream:levelone")
NASDAQ_BOOK_KEY = os.getenv("SCHWAB_NASDAQ_BOOK_HASH_KEY", "schwab:stream:nasdaq_book")
NYSE_BOOK_KEY = os.getenv("SCHWAB_NYSE_BOOK_HASH_KEY", "schwab:stream:nyse_book")
CHART_KEY_PREFIX = os.getenv("SCHWAB_CHART_KEY_PREFIX", "schwab:stream:chart:")
r = redis.from_url(REDIS_URL, decode_responses=True)


def _float_env(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _hype_score(candidate: dict[str, Any]) -> float:
    payload = candidate.get("payload") or {}
    semantic = payload.get("semantic_signal") or {}
    hype = semantic.get("hype") or payload.get("hype") or {}
    return _number(hype.get("long_hype_score"))


class ShadowTradeWorker:
    def __init__(self) -> None:
        self.interval = max(0.5, _float_env("SHADOW_TRADE_INTERVAL_SECONDS", 1.0))
        self.candidate_max_age = max(30, int(os.getenv("SHADOW_CANDIDATE_MAX_AGE_SECONDS", "900")))
        self.minimum_hype = max(0.0, min(_float_env("SHADOW_MINIMUM_HYPE_SCORE", 0.25), 1.0))
        self.max_spread_bps = max(1.0, _float_env("SHADOW_MAX_SPREAD_BPS", 100.0))
        self.slippage_bps = max(0.0, _float_env("SHADOW_SLIPPAGE_BPS", 10.0))
        self.synthetic_equity = max(100.0, _float_env("SHADOW_SYNTHETIC_EQUITY", 10_000.0))
        self.stream_limits = ExitStreamLimits(
            max_quote_age_seconds=_float_env("AGENT_MAX_QUOTE_AGE_SECONDS", 3.0),
            max_receive_age_seconds=_float_env("AGENT_MAX_QUOTE_AGE_SECONDS", 3.0),
            max_book_age_seconds=_float_env("AGENT_MAX_LEVEL2_AGE_SECONDS", 2.0),
            max_book_receive_age_seconds=_float_env("AGENT_MAX_LEVEL2_RECEIVE_AGE_SECONDS", 2.0),
        )
        self.exit_policy = ExitPolicy(
            partial_profit_r=_float_env("AGENT_PARTIAL_PROFIT_R", 1.5),
            partial_fraction=_float_env("AGENT_PARTIAL_FRACTION", 0.5),
            break_even_r=_float_env("AGENT_BREAK_EVEN_R", 1.0),
            runner_activation_r=_float_env("AGENT_RUNNER_ACTIVATION_R", 2.0),
            runner_trail_r=_float_env("AGENT_RUNNER_TRAIL_R", 0.75),
            order_flow_exit_threshold=_float_env("AGENT_ORDER_FLOW_EXIT_THRESHOLD", -0.35),
        )
        self.risk_limits = RiskLimits(
            risk_per_trade_pct=_float_env("AGENT_RISK_PER_TRADE_PCT", 0.005),
            daily_loss_limit_pct=_float_env("AGENT_DAILY_LOSS_LIMIT_PCT", 0.02),
            max_aggregate_open_risk_pct=_float_env("AGENT_MAX_AGGREGATE_OPEN_RISK_PCT", 0.015),
            max_position_pct=_float_env("AGENT_MAX_POSITION_PCT", 0.10),
            max_volume_participation_pct=_float_env("AGENT_MAX_VOLUME_PARTICIPATION_PCT", 0.005),
            slippage_bps=self.slippage_bps,
        )

    def _heartbeat(self, status: str, **extra: Any) -> None:
        try:
            r.set(HEARTBEAT_KEY, json.dumps({"status": status, "ts": int(time.time()), **extra}))
        except Exception:
            pass

    def _snapshot(self, symbol: str) -> dict[str, Any]:
        return load_market_stream_snapshot(
            r, symbol=symbol, levelone_key=LEVELONE_KEY,
            nasdaq_book_key=NASDAQ_BOOK_KEY, nyse_book_key=NYSE_BOOK_KEY,
            chart_key_prefix=CHART_KEY_PREFIX, chart_limit=10,
        )

    def _reject(self, candidate: dict[str, Any], reason: str) -> None:
        record_candidate_disposition(candidate, status="rejected", reason=reason)
        opportunity_status = "expired" if reason == "candidate_expired" else "watching"
        record_opportunity_disposition(
            candidate, status=opportunity_status, reason=reason,
        )

    def _monitor(self, candidate: dict[str, Any], reason: str) -> None:
        record_candidate_disposition(candidate, status="monitoring", reason=reason)
        record_opportunity_disposition(
            candidate, status="monitoring", reason=reason,
        )

    def _evaluate_candidates(self, now: int, counts: dict[str, int]) -> None:
        active = list_active_positions()
        aggregate_risk = sum(
            max(_number(row["entry_price"]) - _number(row["hard_stop_price"]), 0)
            * int(row["remaining_quantity"])
            for row in active
        )
        for candidate in list_pending_opportunities(
            since_ts=now - 86_400,
            expire_before_ts=now - self.candidate_max_age,
            limit=100,
        ):
            counts["candidates_checked"] += 1
            payload = candidate.get("payload") or {}
            age = now - int(candidate["ts"])
            if age > self.candidate_max_age:
                self._reject(candidate, "candidate_expired")
                counts["candidates_rejected"] += 1
                continue
            if payload.get("shadow_stage") != "actionable":
                self._reject(candidate, "scanner_stage_not_actionable")
                counts["candidates_rejected"] += 1
                continue
            hype = _hype_score(candidate)
            if hype < self.minimum_hype:
                self._reject(candidate, "hype_below_threshold")
                counts["candidates_rejected"] += 1
                continue
            snapshot = self._snapshot(candidate["symbol"])
            quote = snapshot.get("levelone") or {}
            bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
            midpoint = (bid + ask) / 2 if bid > 0 and ask >= bid else 0
            spread_bps = (ask - bid) / midpoint * 10_000 if midpoint else 0
            received_ms = int(quote.get("received_at_ms") or 0)
            event_ms = max(int(quote.get(key) or 0) for key in ("quote_time_ms", "trade_time_ms"))
            if (
                midpoint <= 0 or received_ms <= int(candidate["ts"]) * 1000
                or snapshot.get("levelone_receive_age_seconds") is None
                or _number(snapshot["levelone_receive_age_seconds"]) > self.stream_limits.max_receive_age_seconds
                or quote.get("delayed") is True
                or str(quote.get("security_status") or "").lower() not in {"", "normal"}
                or spread_bps > self.max_spread_bps
            ):
                self._monitor(candidate, "entry_quote_invalid_or_stale")
                counts["candidates_monitoring"] += 1
                continue
            metrics = snapshot.get("book_metrics") or {}
            if (
                metrics.get("service") not in {"NASDAQ_BOOK", "NYSE_BOOK"}
                or metrics.get("receive_age_seconds") is None
                or _number(metrics["receive_age_seconds"]) > self.stream_limits.max_book_receive_age_seconds
                or _number(metrics.get("ask_depth")) <= 0
            ):
                self._monitor(candidate, "entry_book_invalid_or_stale")
                counts["candidates_monitoring"] += 1
                continue
            entry_price = ask * (1 + self.slippage_bps / 10_000)
            plan = payload.get("trade_plan") or {}
            stop = _number(plan.get("stop_loss") or plan.get("stop"))
            if not 0 < stop < entry_price:
                stop = entry_price * 0.98
            candles = snapshot.get("candles") or []
            average_volume = (
                sum(_number(row.get("volume")) for row in candles) / len(candles)
                if candles else None
            )
            sizing = calculate_long_position_size(
                account_equity=self.synthetic_equity,
                buying_power=self.synthetic_equity,
                entry_price=entry_price,
                stop_price=stop,
                aggregate_open_risk=aggregate_risk,
                average_minute_volume=average_volume,
                limits=self.risk_limits,
            )
            ask_cap = max(0, math.floor(_number(quote.get("ask_size_shares")) * 0.25))
            quantity = min(sizing.quantity, ask_cap)
            if not sizing.allowed or quantity < 1:
                self._monitor(candidate, sizing.reason or "ask_liquidity_below_one_share")
                counts["candidates_monitoring"] += 1
                continue
            position = open_position(
                candidate, quantity=quantity, entry_price=entry_price,
                hard_stop_price=stop, event_ts=(event_ms or received_ms) // 1000,
                received_ts=received_ms // 1000,
                payload={"hype_score": hype, "spread_bps": spread_bps,
                         "book_metrics": metrics, "sizing": sizing.to_dict(),
                         "execution_mode": "shadow", "broker_calls": 0},
            )
            if position:
                aggregate_risk += quantity * (entry_price - stop)
                counts["positions_opened"] += 1
            else:
                counts["candidates_skipped"] += 1

    def _evaluate_positions(self, now: int, counts: dict[str, int]) -> None:
        for position in list_active_positions():
            counts["positions_checked"] += 1
            snapshot = self._snapshot(position["symbol"])
            evaluation = evaluate_position_exit(
                {**position, "average_entry_price": position["entry_price"]},
                snapshot, policy=self.exit_policy, limits=self.stream_limits,
            ).to_dict()
            if evaluation["quote_is_stale"]:
                counts["stale_holds"] += 1
                continue
            quote = snapshot.get("levelone") or {}
            bid = _number(quote.get("bid"))
            received_ms = int(quote.get("received_at_ms") or 0)
            fill_price = None
            if evaluation["action"] in {"take_partial", "exit_all"} and bid > 0:
                fill_price = bid * (1 - self.slippage_bps / 10_000)
            apply_evaluation(
                position["position_id"], evaluation=evaluation, fill_price=fill_price,
                event_ts=received_ms // 1000 or now,
            )
            counts[evaluation["action"] if evaluation["action"] in counts else "hold"] += 1

    def run_once(self, *, now: int | None = None) -> dict[str, Any]:
        current = int(now or time.time())
        counts = {
            "candidates_checked": 0, "candidates_rejected": 0,
            "candidates_monitoring": 0, "candidates_skipped": 0,
            "positions_opened": 0,
            "positions_checked": 0, "hold": 0, "take_partial": 0,
            "exit_all": 0, "blocked": 0, "stale_holds": 0,
        }
        session = market_session(datetime.fromtimestamp(current, tz=timezone.utc))
        if session["is_regular"]:
            self._evaluate_positions(current, counts)
        counts["opportunities_expired"] = expire_stale_opportunities(
            expire_before_ts=current - self.candidate_max_age,
        )
        if session["is_regular"]:
            self._evaluate_candidates(current, counts)
        return {**counts, "market_session": session["state"], **summary()}

    def run_forever(self) -> None:
        ensure_schema()
        while True:
            try:
                self._heartbeat("ok", **self.run_once())
            except Exception as exc:
                logger.exception("shadow trade cycle failed")
                self._heartbeat("error", error=str(exc), execution_mode="shadow", broker_calls=0)
            time.sleep(self.interval)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    ShadowTradeWorker().run_forever()
