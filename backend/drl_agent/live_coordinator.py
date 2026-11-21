"""Synchronous shadow-training coordinator; no broker orders or background loop."""
from dataclasses import replace
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import math
from zoneinfo import ZoneInfo

import torch

from drl_agent.live_checkpoint import restore_checkpoint, save_checkpoint
from drl_agent.live_execution import LiveShadowExecution
from drl_agent.live_features import encode_live
from drl_agent.live_inputs import scanner_admission
from drl_agent.live_recurrent import RecurrentPPO, TARGET_EXPOSURES
from drl_agent.live_reward import RewardConfig, session_bounds
from drl_agent.live_rollout import LiveRolloutCollector
from drl_agent.live_stream_adapter import LiveStreamAdapter
from drl_agent.simulator import SimulationConfig


class LiveCoordinator:
    def __init__(self, symbols, *, session_date, watch_config, checkpoint, max_steps=256):
        self.symbols = tuple(symbols)
        self.adapter = LiveStreamAdapter(self.symbols)
        if len(self.symbols) > watch_config.capacity:
            raise ValueError('Roster exceeds watch capacity')
        self.watch_config = watch_config
        self.path = Path(checkpoint)
        self.reward_config = RewardConfig()
        self.inactive_accounts = {}
        base = encode_live(dict(symbol=self.symbols[0], decision_ts=1, inputs={}), {})
        self.feature_names = base['names'] + ['valid:' + name for name in base['names']]
        if self.path.exists():
            restored = restore_checkpoint(self.path, feature_names=self.feature_names, reward_config=self.reward_config)
            self.agent, self.executions = restored['agent'], restored['executions']
            self.inactive_accounts = restored['inactive_accounts']
            if tuple(e.symbol for e in self.executions) != self.symbols:
                raise ValueError('Checkpoint roster differs')
            if any(e.config.minimum_price != watch_config.minimum_price or e.config.maximum_price != watch_config.maximum_price
                   for e in self.executions):
                raise ValueError('Checkpoint price settings differ')
            peaks = restored['peak_equity']
        else:
            self.agent = RecurrentPPO(len(self.feature_names), max_steps=max_steps, max_symbols=min(50, watch_config.capacity))
            config = replace(SimulationConfig(), initial_cash=500,
                minimum_price=watch_config.minimum_price, maximum_price=watch_config.maximum_price)
            self.executions = [LiveShadowExecution(s, session_date, config=config) for s in self.symbols]
            peaks = torch.full((len(self.symbols),), 500.)
        self.collector = LiveRolloutCollector(self.agent, self.symbols, reward_config=self.reward_config)
        self.collector.peak_equity = peaks
        self.blocked = False
        self.last_timestamp = 0.
        self.transitions = 0
        self.state = 'waiting_for_market'

    def _save(self):
        peaks = torch.maximum(self.collector.peak_equity,
                              torch.tensor([e.equity for e in self.executions]))
        self.collector.peak_equity = peaks
        save_checkpoint(self.path, self.agent, self.executions, feature_names=self.feature_names,
            reward_config=self.reward_config, peak_equity=peaks, inactive_accounts=self.inactive_accounts)

    def reconfigure(self, symbols, *, session_date):
        """Atomically change active columns without erasing inactive account history."""
        if self.blocked:
            raise RuntimeError('Coordinator is blocked')
        symbols = tuple(symbols)
        adapter = LiveStreamAdapter(symbols)
        if len(symbols) > min(self.agent.max_symbols, self.watch_config.capacity):
            raise ValueError('Roster exceeds capacity')
        held = {e.symbol for e in self.executions if e.quantity}
        if not held.issubset(symbols):
            raise ValueError('Cannot remove held symbols')
        if symbols == self.symbols:
            return False
        active = {e.symbol: dict(execution=e, peak=max(float(p), e.equity, e.config.initial_cash))
                  for e, p in zip(self.executions, self.collector.peak_equity)}
        archive = dict(self.inactive_accounts)
        for symbol, item in active.items():
            if symbol not in symbols:
                archived = deepcopy(item)
                archived['execution'].pending = None
                archived['execution'].latest = None
                archive[symbol] = archived
        accounts, peaks = [], []
        for symbol in symbols:
            item = active.get(symbol) or archive.pop(symbol, None)
            if item is None:
                config = replace(SimulationConfig(), initial_cash=500,
                    minimum_price=self.watch_config.minimum_price, maximum_price=self.watch_config.maximum_price)
                execution, peak = LiveShadowExecution(symbol, session_date, config=config), 500.
            else:
                execution, peak = deepcopy(item['execution']), item['peak']
                execution.latest = None
                if execution.pending and not execution.pending['protective']:
                    execution.pending = None
                if execution.session_date != session_date:
                    # Validate the requested trading date before settling the copy.
                    next_session = LiveShadowExecution(symbol, session_date, config=execution.config)
                    if next_session.open <= execution.close:
                        raise ValueError('Roster session cannot move backwards')
                    execution.settle_session(execution.close)
                    execution.start_session(session_date)
            accounts.append(execution)
            peaks.append(peak)
        collector = LiveRolloutCollector(self.agent, symbols, reward_config=self.reward_config)
        collector.peak_equity = torch.tensor(peaks)
        # Persist the new mapping before exposing it; a failed write leaves the
        # original coordinator and roster intact, including its pending actions.
        save_checkpoint(self.path, self.agent, accounts, feature_names=self.feature_names,
            reward_config=self.reward_config, peak_equity=peaks, inactive_accounts=archive)
        self.symbols, self.executions, self.inactive_accounts = symbols, accounts, archive
        self.adapter, self.collector = adapter, collector
        self.state = 'waiting_for_market'
        return True

    def _report(self, **details):
        return dict(status=self.state, policy_version=self.agent.version,
            process_training_transitions=self.transitions, broker_calls=0,
            active_accounts=len(self.executions), inactive_accounts=len(self.inactive_accounts),
            account_scope='independent_symbol_simulations', **details)

    def tick(self, *, timestamp, snapshots, scanner_records):
        if self.blocked:
            return self._report()
        if isinstance(timestamp, bool) or not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError('Invalid tick timestamp')
        if timestamp <= self.last_timestamp:
            return self._report(reason='duplicate_or_old_tick')
        self.last_timestamp = timestamp
        day = datetime.fromtimestamp(timestamp, ZoneInfo('America/New_York')).date()
        bounds = session_bounds(day)
        try:
            for execution in self.executions:
                if timestamp >= execution.close and execution.session_report is None:
                    execution.settle_session(timestamp)
                    self.collector.reset('session_closed')
                    self._save()
            if bounds is None or not bounds[0] <= timestamp < bounds[1]:
                self.state = 'market_closed'
                return self._report()
            for execution in self.executions:
                if execution.session_date != day.isoformat():
                    execution.start_session(day.isoformat())
                    self.collector.reset('new_session')
            prepared, errors = {}, {}
            for execution in self.executions:
                symbol = execution.symbol
                try:
                    # Invalid scanner evidence must not prevent management of a held position.
                    snapshot = snapshots[symbol]
                    admission = scanner_admission(scanner_records.get(symbol), symbol=symbol,
                        timestamp=timestamp, current_price=(snapshot.get('levelone') or {}).get('ask'), config=self.watch_config)
                    prepared[symbol] = (self.adapter.adapt(snapshot, timestamp=timestamp, account={},
                        scanner_record=scanner_records.get(symbol) if admission['watch_allowed'] else None), admission)
                    if not admission['watch_allowed'] and not execution.quantity:
                        errors[symbol] = 'watch_candidate_unavailable'
                except (ValueError, KeyError, TypeError) as exc:
                    errors[symbol] = str(exc)
                    if execution.pending and not execution.pending['protective']:
                        execution.pending = None
            events, observations, masks = {}, [], []
            for execution in self.executions:
                if execution.symbol not in prepared:
                    continue
                inputs, admission = prepared[execution.symbol]
                if not admission['entry_inputs_ready'] and execution.pending and execution.pending['target'] > execution.quantity:
                    execution.pending = None
                event = execution.advance(inputs['execution_quote'])
                events[execution.symbol] = event
                inventory = event['inventory_fraction']
                peak = max(500., float(self.collector.peak_equity[self.symbols.index(execution.symbol)]), execution.equity)
                account = dict(observed_ts=timestamp, exposure_fraction=inventory,
                    unrealized_return=(execution.equity - execution.cash - execution.quantity * execution.cost_basis) / 500,
                    drawdown_fraction=(peak - execution.equity) / 500)
                features = encode_live(inputs['state'], account)
                observations.append(features['values'] + features['valid'])
                q = inputs['execution_quote']
                price = q.ask * (1 + execution.config.slippage_bps / 10000)
                stop = execution.stop if execution.quantity else price * (1 - execution.config.stop_fraction)
                size = execution._size(q, price, stop)
                mask = [True] * 4
                if execution.pending and execution.pending['protective'] or timestamp >= execution.close - 300:
                    mask = [True, False, False, False]
                elif not admission['entry_inputs_ready']:
                    mask = [int(size * target) <= execution.quantity for target in TARGET_EXPOSURES]
                masks.append(mask)
            if any(event.get('fill') for event in events.values()):
                self._save()
            if errors:
                self.collector.reset('input_unavailable')
                self.state = 'waiting_for_inputs'
                return self._report(errors=errors, execution=events)
            result = self.collector.observe(symbols=self.symbols, timestamp=timestamp,
                observations=observations, masks=masks,
                quote_times=[prepared[s][0]['execution_quote'].event_ts for s in self.symbols],
                book_times=[prepared[s][0]['execution_quote'].book_event_ts for s in self.symbols],
                equity=[e.equity for e in self.executions],
                inventory_fraction=[events[s]['inventory_fraction'] for s in self.symbols], session_open=True)
            if result and 'rollout' in result:
                update = self.agent.update(result['rollout'])
                self.transitions += update['transitions']
                self._save()
                self.state = 'updated'
                return self._report(update=update, execution=events)
            if result and 'actions' in result:
                for execution, action in zip(self.executions, result['actions']):
                    execution.propose(int(action))
            self.state = 'collecting' if result else self.collector.status
            return self._report(execution=events,
                admission={s: prepared[s][1] for s in self.symbols})
        except Exception:
            self.blocked = True
            self.state = 'blocked_error'
            for execution in self.executions:
                if execution.pending and not execution.pending['protective']:
                    execution.pending = None
            raise
