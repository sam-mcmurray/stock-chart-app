"""Offline, single-symbol-per-session synthetic mechanics competitions."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib

from drl_agent.dataset import encode_dataset
from drl_agent.simulator import SimulationConfig
from drl_agent.synthetic_execution import SyntheticExecution


def _account(sim):
    bids = sim.books[sim.states[sim.index]["candle_id"]]["bids"]
    bid = next((x["price"] for x in bids if x["size"]), None)
    # No executable bid means an unpriced position, not a zero-dollar loss.
    equity = (None if sim.quantity and bid is None else sim.cash + sim.quantity * (bid or 0)
              * (1 - sim.config.slippage_bps / 10000) * (1 - sim.config.fee_bps / 10000))
    return {"cash": sim.cash, "equity": equity, "quantity": sim.quantity,
            "entry": sim.entry, "active_stop": sim.active_stop,
            "pending_exit": sim.pending_exit, "realized_pnl": sim.realized,
            "settled_buying_power": max(0, min(sim.cash, sim.config.initial_cash - sim.purchase_cost))}


def compare_synthetic_sessions(sessions, factories):
    """Sessions are {date, joined}; factories create observation -> order policies.

    Factories reset at each week boundary. Observations expose only the current
    state, completed candle/book and active news, never the full future artifact.
    Explicit orders exercise mechanics; this is not historical Scanner admission.
    """
    if not sessions or len(factories) < 2 or "stay-out" in factories:
        raise ValueError("Provide sessions and two competitors; stay-out is reserved")
    dates = [date.fromisoformat(s["date"]) for s in sessions]
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError("Sessions must be distinct dates in chronological order")
    # Validate every input even if a competitor stops before reaching it.
    for session in sessions:
        SyntheticExecution(session["joined"], session_date=session["date"])
    competitors = {}
    for name, factory in {"stay-out": lambda: lambda obs: {}, **factories}.items():
        cash, previous_week, policy = 500., None, None
        daily, weeks, warnings = [], {}, []
        for day, session in zip(dates, sessions):
            year, week, _ = day.isocalendar()
            key = f"{year}-W{week:02d}"
            reset = previous_week is not None and previous_week != key
            adjustment = 500 - cash if reset else 0.
            if key != previous_week:
                cash, policy = 500., factory()
            previous_week = key
            sim = SyntheticExecution(session["joined"], session_date=session["date"],
                                     config=replace(SimulationConfig(), initial_cash=cash))
            articles = {a["candidate_id"]: a for a in session["joined"]["articles"]}
            weekly = weeks.setdefault(key, {"week": key, "starting_equity": 500.,
                "net_pnl": 0., "fees": 0., "round_trips": 0, "sessions": 0,
                "peak_equity": 500., "max_drawdown": 0., "complete": True})
            peak, drawdown, valuation_missing = cash, 0., False
            while not sim.done:
                state = sim.states[sim.index]
                active = [articles[i] for i in state["news_candidate_ids"]]
                if any(a["replay_available_ts"] > state["decision_ts"] for a in active):
                    raise ValueError("Future article in current state")
                observation = deepcopy({"state": state, "account": _account(sim),
                    "candle": sim.candles[state["candle_id"]],
                    "book": sim.books[state["candle_id"]], "articles": active})
                sim.step(**policy(observation))
                equity = _account(sim)["equity"]
                if equity is None:
                    valuation_missing = True
                else:
                    peak = max(peak, equity)
                    drawdown = max(drawdown, peak - equity)
                    weekly["peak_equity"] = max(weekly["peak_equity"], equity)
                    weekly["max_drawdown"] = max(weekly["max_drawdown"], weekly["peak_equity"] - equity)
            account = _account(sim)
            pnl = account["equity"] - cash if account["equity"] is not None else None
            daily.append({"date": day.isoformat(), "symbol": sim.symbol, "starting_equity": cash,
                "ending_equity": account["equity"], "net_pnl": pnl, "fees": sim.fees,
                "round_trips": sim.completed_round_trips, "max_drawdown": drawdown,
                "risk_adjusted_score": ((pnl - .1 * drawdown) / cash
                    if pnl is not None and not valuation_missing and not sim.quantity else None),
                "valuation_incomplete": valuation_missing, "weekly_capital_reset": reset,
                "capital_adjustment": adjustment, "account": account,
                "source_hash": sim.source_hash, "events": sim.events})
            weekly.update(ending_equity=account["equity"],
                net_pnl=weekly["net_pnl"] + pnl if pnl is not None else None,
                fees=weekly["fees"] + sim.fees,
                round_trips=weekly["round_trips"] + sim.completed_round_trips,
                sessions=weekly["sessions"] + 1)
            if valuation_missing:
                warnings.append("missing_executable_bid_valuation")
                weekly["complete"] = False
            if sim.quantity:
                warnings.append("unresolved_position_at_session_end")
                weekly["complete"] = False
                break
            cash = sim.cash
        for weekly in weeks.values():
            weekly["risk_adjusted_score"] = ((weekly["net_pnl"] - .1 * weekly["max_drawdown"]) / 500
                                             if weekly["complete"] else None)
        competitors[name] = {"daily": daily, "weekly": list(weeks.values()),
                             "warnings": sorted(set(warnings)),
                             "complete": len(daily) == len(sessions) and not warnings}
    result = {"version": "synthetic-competition-v1", "competitors": competitors,
        "requested_dates": [d.isoformat() for d in dates], "weekly_starting_cash": 500,
        "weekly_reset_basis": "New York ISO week; first supplied session",
        "frequency_cap_enabled": False, "synthetic": True, "training_ready": False,
        "promotion_allowed": False, "broker_calls": 0,
        "limitations": ["Single symbol per session; not a shared multi-symbol portfolio",
            "Explicit mechanics policies, not certified historical Scanner eligibility",
            "Flat-session cash carries forward; unresolved inventory stops the competitor",
            "Scores describe supplied sessions only, not necessarily complete calendar weeks",
            "Buying power resets each supplied session; historical settlement regimes are not modeled",
            "Synthetic minute-close execution and retrospective news are not live evidence"]}
    result["report_sha256"] = hashlib.sha256(encode_dataset(result).encode()).hexdigest()
    return result
