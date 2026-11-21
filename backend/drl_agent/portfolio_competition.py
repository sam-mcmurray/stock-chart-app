"""Daily and weekly offline comparisons; flat-session cash/history continuity."""
from dataclasses import asdict, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from drl_agent.portfolio_replay import PortfolioReplay
from drl_agent.simulator import Action, SimulationConfig


def compare_portfolio_sessions(sessions, factories, *, watch_config, replay_factory=PortfolioReplay):
    if not sessions or len(factories) < 2 or "stay-out" in factories:
        raise ValueError("Provide sessions and two competitors; stay-out is reserved")
    dates = []
    for datasets in sessions:
        if isinstance(datasets, dict):
            date = datetime.strptime(datasets["date"], "%Y-%m-%d").date()
        elif not datasets or not datasets[0].get("states"):
            raise ValueError("Empty session")
        else:
            date = datetime.fromtimestamp(datasets[0]["states"][0]["decision_ts"], ZoneInfo("America/New_York")).date()
        if dates and date <= dates[-1]:
            raise ValueError("Sessions must be distinct dates in chronological order")
        dates.append(date)
    output = {}
    for name, factory in {"stay-out": lambda: lambda obs: {}, **factories}.items():
        policy = factory()
        cash, peak, max_drawdown = 500.0, 500.0, 0.0
        history, daily, weeks = [], [], {}
        previous_week = None
        for date, datasets in zip(dates, sessions):
            year, week, _ = date.isocalendar()
            week_key = f"{year}-W{week:02d}"
            reset = previous_week is not None and week_key != previous_week
            capital_adjustment = 500.0 - cash if reset else 0.0
            if reset:
                cash = peak = 500.0
            previous_week = week_key
            replay = replay_factory(datasets, watch_config=watch_config,
                                     config=replace(SimulationConfig(), initial_cash=cash),
                                     prior_round_trip_timestamps=history)
            observation = replay.observation()
            weekly = weeks.setdefault(week_key, {"week": week_key, "starting_equity": cash,
                                                "ending_equity": cash, "net_pnl": 0.0,
                                                "sessions": 0, "positive_sessions": 0,
                                                "round_trips": 0, "fees": 0.0,
                                                "peak_equity": cash, "max_drawdown": 0.0})
            while not replay.done:
                observation, _, _, _, event = replay.step(policy(observation))
                equity = event["account"]["equity"]
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
                weekly["peak_equity"] = max(weekly["peak_equity"], equity)
                weekly["max_drawdown"] = max(weekly["max_drawdown"], weekly["peak_equity"] - equity)
            account = replay.account()
            trades, active = [], {}
            for event in replay.events:
                for symbol, transition in event["symbols"].items():
                    fill = transition["fill"]
                    if not fill:
                        continue
                    trade = active.setdefault(symbol, {"symbol": symbol,
                        "entry_ts": event["evaluation_ts"], "net_pnl": 0.0, "fees": 0.0})
                    value = fill["quantity"] * fill["price"]
                    trade["net_pnl"] += (-value if fill["side"] == "buy" else value) - fill["fee"]
                    trade["fees"] += fill["fee"]
                    if fill["side"] == "sell" and not transition["quantity"]:
                        trades.append({**trade, "exit_ts": event["evaluation_ts"]})
                        del active[symbol]
            pnl = account["equity"] - cash
            completed = len(replay.round_trip_timestamps) - len(history)
            summary = {"date": date.isoformat(), "starting_equity": cash,
                       "weekly_capital_reset": reset, "capital_adjustment": capital_adjustment,
                       "ending_equity": account["equity"], "net_pnl": pnl,
                       "max_drawdown": account["max_drawdown"], "fees": account["fees"],
                       "round_trips": completed, "open_positions": account["positions"],
                       "fills": sum(t["fill"] is not None for e in replay.events for t in e["symbols"].values()),
                       "source_hashes": replay.source_hashes,
                       "closed_trades": trades,
                       "winning_trades": sum(t["net_pnl"] > 0 for t in trades),
                       "losing_trades": sum(t["net_pnl"] < 0 for t in trades),
                       "risk_adjusted_score": sum(e["reward"] for e in replay.events)}
            daily.append(summary)
            weekly.update(ending_equity=account["equity"], net_pnl=weekly["net_pnl"] + pnl,
                          sessions=weekly["sessions"] + 1, positive_sessions=weekly["positive_sessions"] + int(pnl > 0),
                          round_trips=weekly["round_trips"] + completed, fees=weekly["fees"] + account["fees"])
            if account["positions"]:
                # Never erase or fabricate liquidation of unresolved exposure
                # just to begin another evaluation day.
                break
            cash, history = account["cash"], replay.round_trip_timestamps
        for weekly in weeks.values():
            weekly["risk_adjusted_score"] = (weekly["net_pnl"] - 0.1 * weekly["max_drawdown"]) / weekly["starting_equity"]
        closed = [trade for row in daily for trade in row["closed_trades"]]
        output[name] = {"daily": daily, "weekly": list(weeks.values()),
                        "closed_trades": len(closed),
                        "winning_trades": sum(t["net_pnl"] > 0 for t in closed),
                        "losing_trades": sum(t["net_pnl"] < 0 for t in closed),
                        "win_rate": sum(t["net_pnl"] > 0 for t in closed) / len(closed) if closed else None,
                        "max_drawdown": max_drawdown,
                        "warnings": (["unresolved_position_at_session_end"] if daily[-1]["open_positions"] else [])
                                    + (["no_executions"] if not any(d["fills"] for d in daily) else []),
                        "complete": len(daily) == len(sessions) and not daily[-1]["open_positions"]}
    return {"version": "drl-portfolio-competition-v3", "competitors": output,
            "requested_dates": [d.isoformat() for d in dates],
            "starting_cash": 500, "maximum_round_trips_per_rolling_week": None,
            "weekly_starting_cash": 500, "weekly_reset_timezone": "America/New_York",
            "weekly_reset_basis": "ISO week; first supplied session",
            "frequency_cap_enabled": False,
            "watch_config": asdict(watch_config),
            "simulation_config": asdict(SimulationConfig(initial_cash=500)),
            "promotion_allowed": False, "training_started": False, "broker_calls": 0}


class EnterExitPolicy:
    def __call__(self, observation):
        actions = {c["symbol"]: Action.ENTER for c in observation["watching"]}
        actions.update({s: Action.EXIT for s in observation["account"]["positions"]})
        return actions
