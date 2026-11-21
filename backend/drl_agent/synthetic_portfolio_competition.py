"""Daily/weekly competitions using one shared synthetic account per competitor."""
from datetime import date
import hashlib

from drl_agent.dataset import encode_dataset
from drl_agent.simulator import SimulationConfig
from drl_agent.synthetic_portfolio import SyntheticPortfolio


def compare_synthetic_portfolio_sessions(sessions, factories):
    if not sessions or len(factories) < 2 or "stay-out" in factories:
        raise ValueError("Provide sessions and two competitors; stay-out is reserved")
    dates = [date.fromisoformat(s["date"]) for s in sessions]
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError("Sessions must be distinct dates in chronological order")
    for session in sessions:
        SyntheticPortfolio(session["joined"], session_date=session["date"], watch=session.get("watch"))
    output = {}
    for name, factory in {"stay-out": lambda: lambda obs: {}, **factories}.items():
        cash, previous_week = 500., None
        daily, weeks, warnings = [], {}, set()
        for day, session in zip(dates, sessions):
            year, week, _ = day.isocalendar()
            key = f"{year}-W{week:02d}"
            reset = previous_week is not None and previous_week != key
            adjustment = 500 - cash if reset else 0.
            if key != previous_week:
                cash, policy = 500., factory()
            previous_week = key
            sim = SyntheticPortfolio(session["joined"], session_date=session["date"],
                                     config=SimulationConfig(initial_cash=cash), watch=session.get("watch"))
            weekly = weeks.setdefault(key, {"week": key, "starting_equity": 500.,
                "net_pnl": 0., "fees": 0., "round_trips": 0, "sessions": 0,
                "peak_equity": 500., "max_drawdown": 0., "complete": True})
            while not sim.done:
                event = sim.step(policy(sim.observation()))
                equity = event["account"]["equity"]
                if equity is not None:
                    weekly["peak_equity"] = max(weekly["peak_equity"], equity)
                    weekly["max_drawdown"] = max(weekly["max_drawdown"], weekly["peak_equity"] - equity)
            account = sim.account()
            pnl = account["equity"] - cash if account["equity"] is not None else None
            complete = not account["positions"] and not sim.valuation_incomplete
            daily.append({"date": day.isoformat(), "starting_equity": cash,
                "ending_equity": account["equity"], "net_pnl": pnl,
                "weekly_capital_reset": reset, "capital_adjustment": adjustment,
                "risk_adjusted_score": (pnl - .1 * sim.max_drawdown) / cash if complete else None,
                "replay": sim.report()})
            weekly.update(ending_equity=account["equity"],
                net_pnl=weekly["net_pnl"] + pnl if pnl is not None else None,
                fees=weekly["fees"] + account["fees"],
                round_trips=weekly["round_trips"] + account["completed_round_trips"],
                sessions=weekly["sessions"] + 1, complete=weekly["complete"] and complete)
            if sim.valuation_incomplete:
                warnings.add("missing_executable_bid_valuation")
            if account["positions"]:
                warnings.add("unresolved_position_at_session_end")
                break
            cash = account["cash"]
        for weekly in weeks.values():
            weekly["risk_adjusted_score"] = ((weekly["net_pnl"] - .1 * weekly["max_drawdown"]) / 500
                                             if weekly["complete"] else None)
        output[name] = {"daily": daily, "weekly": list(weeks.values()), "warnings": sorted(warnings),
                        "complete": len(daily) == len(sessions) and not warnings}
    report = {"version": "synthetic-portfolio-competition-v1", "competitors": output,
              "requested_dates": [d.isoformat() for d in dates], "weekly_starting_cash": 500,
              "weekly_reset_basis": "New York ISO week; first supplied session",
              "synthetic": True, "frequency_cap_enabled": False, "training_ready": False,
              "promotion_allowed": False, "broker_calls": 0,
              "limitations": ["Explicit diagnostic intents, not certified historical Scanner admission",
                  "Matched decision grids required; incomplete weeks are not full-week performance",
                  "No fabricated liquidation or transfer of unresolved overnight inventory",
                  "Buying power resets each supplied session; historical settlement regimes are not modeled",
                  "Retrospective news and synthetic minute-close execution are not live evidence"]}
    report["report_sha256"] = hashlib.sha256(encode_dataset(report).encode()).hexdigest()
    return report
