"""Atomic CPU research checkpoints. Restore starts a new bounded rollout window."""
from dataclasses import asdict
import math
import os
from pathlib import Path
import tempfile

import torch

from drl_agent.live_execution import LiveShadowExecution
from drl_agent.live_features import VERSION as FEATURE_VERSION
from drl_agent.live_recurrent import RecurrentPPO, VERSION as POLICY_VERSION
from drl_agent.live_reward import RewardConfig
from drl_agent.simulator import SimulationConfig

VERSION = 'live-research-checkpoint-v3'
ACCOUNT_FIELDS = ('cash', 'equity', 'quantity', 'entry', 'cost_basis', 'stop', 'active_stop',
                  'peak', 'realized', 'fees', 'purchased', 'partial_taken', 'last_fill_event',
                  'daily_realized_baseline')


def _execution_state(execution):
    return dict(symbol=execution.symbol, session_date=execution.session_date,
        config=asdict(execution.config), risk=asdict(execution.risk),
        account={k: getattr(execution, k) for k in ACCOUNT_FIELDS},
        session_report=execution.session_report, carry_boundary=execution.carry_boundary)


def _restore_execution(saved):
    execution = LiveShadowExecution(saved['symbol'], saved['session_date'], config=SimulationConfig(**saved['config']))
    if saved['risk'] != asdict(execution.risk):
        raise ValueError('Checkpoint risk limits do not match')
    account = saved['account']
    if set(account) != set(ACCOUNT_FIELDS):
        raise ValueError('Invalid account fields')
    for key, value in account.items():
        if key == 'partial_taken':
            if type(value) is not bool:
                raise ValueError('Invalid partial-exit flag')
        elif key == 'quantity':
            if type(value) is not int or value < 0:
                raise ValueError('Invalid position quantity')
        elif key == 'last_fill_event' and value == -math.inf:
            continue
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Nonfinite account state')
    if (any(account[k] < 0 for k in ('cash', 'fees', 'purchased', 'entry', 'cost_basis', 'stop', 'active_stop', 'peak'))
            or account['purchased'] > execution.config.initial_cash + 1e-6
            or not math.isclose(account['cash'] + account['quantity'] * account['cost_basis'],
                execution.config.initial_cash + account['realized'], abs_tol=1e-5, rel_tol=1e-7)):
        raise ValueError('Inconsistent account ledger')
    if account['quantity'] and not (0 < account['stop'] < account['entry']
            and account['cost_basis'] >= account['entry'] and account['active_stop'] >= account['stop']):
        raise ValueError('Invalid position protection')
    for key, value in account.items():
        setattr(execution, key, value)
    boundary = saved['carry_boundary']
    if boundary is not None:
        if (set(boundary) != {'from_session', 'reference_equity', 'reference_close', 'quantity', 'inventory_fraction'}
                or not isinstance(boundary['from_session'], str)
                or type(boundary['quantity']) is not int or boundary['quantity'] != execution.quantity
                or execution.quantity <= 0
                or any(isinstance(boundary[k], bool) or not isinstance(boundary[k], (int, float))
                    or not math.isfinite(boundary[k]) for k in ('reference_equity', 'reference_close', 'inventory_fraction'))
                or not 0 < boundary['reference_close'] < execution.open
                or boundary['inventory_fraction'] < 0
                or not math.isclose(boundary['reference_equity'], execution.equity, abs_tol=1e-5)):
            raise ValueError('Invalid overnight boundary')
        execution.carry_boundary = dict(boundary)
    # Never resume an old entry or act from a saved quote. Held positions retain
    # an exit intent, executable only after a newly supplied fresh market frame.
    if execution.quantity:
        execution.pending = dict(target=0, timestamp=execution.open, stop=execution.stop,
            protective=True, reason='restart_flatten')
    report = saved['session_report']
    if report is not None:
        actual = execution.settle_session(execution.close)
        if actual != report:
            raise ValueError('Inconsistent session close report')
    return execution


def _finite_tensors(value):
    if isinstance(value, torch.Tensor):
        if not torch.isfinite(value).all():
            raise ValueError('Nonfinite checkpoint tensor')
    elif isinstance(value, dict):
        for v in value.values():
            _finite_tensors(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _finite_tensors(v)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Nonfinite optimizer setting')


def save_checkpoint(path, agent, executions, *, feature_names, reward_config, peak_equity, inactive_accounts=None):
    """Caller must pause decisions/execution during this coherent snapshot."""
    names = list(feature_names)
    if (not all(isinstance(name, str) and name for name in names)
            or len(names) != agent.policy.encoder[0].in_features or len(set(names)) != len(names)):
        raise ValueError('Invalid feature schema')
    if not executions or len(executions) > agent.max_symbols or len({e.symbol for e in executions}) != len(executions):
        raise ValueError('Invalid account roster')
    peak = torch.as_tensor(peak_equity, dtype=torch.float32).detach().cpu()
    if peak.shape != (len(executions),) or not torch.isfinite(peak).all() or (peak <= 0).any():
        raise ValueError('Invalid reward peak state')
    if any(float(p) + 1e-4 < max(e.equity, e.config.initial_cash) for p, e in zip(peak, executions)):
        raise ValueError('Reward peak below account equity')
    accounts = [_execution_state(e) for e in executions]
    for account in accounts:
        _restore_execution(account)
    inactive = {}
    for symbol, item in (inactive_accounts or {}).items():
        execution, old_peak = item['execution'], item['peak']
        if (symbol != execution.symbol or execution.quantity or symbol in {e.symbol for e in executions}
                or not math.isfinite(old_peak) or old_peak < max(execution.equity, execution.config.initial_cash)):
            raise ValueError('Invalid inactive account')
        state = _execution_state(execution)
        _restore_execution(state)
        inactive[symbol] = dict(execution=state, peak=old_peak)
    if len(inactive) > 5000:
        raise ValueError('Inactive account archive capacity exceeded')
    payload = dict(version=VERSION, feature_version=FEATURE_VERSION, policy_version=POLICY_VERSION,
        feature_names=names, reward_config=asdict(reward_config), peak_equity=peak,
        architecture=dict(input_dim=len(names), hidden_size=agent.policy.hidden_size,
            max_steps=agent.max_steps, max_symbols=agent.max_symbols),
        update_version=agent.version, policy=agent.policy.state_dict(), optimizer=agent.optimizer.state_dict(),
        executions=accounts, inactive_accounts=inactive, rng=torch.get_rng_state())
    _finite_tensors(payload['policy'])
    _finite_tensors(payload['optimizer'])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            torch.save(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore_checkpoint(path, *, feature_names, reward_config):
    saved = torch.load(path, map_location='cpu', weights_only=True)
    if (saved['version'] != VERSION or saved['feature_version'] != FEATURE_VERSION
            or saved['policy_version'] != POLICY_VERSION or saved['feature_names'] != list(feature_names)
            or saved['reward_config'] != asdict(reward_config)):
        raise ValueError('Incompatible model, features or reward configuration')
    if type(saved['update_version']) is not int or saved['update_version'] < 0:
        raise ValueError('Invalid policy update version')
    _finite_tensors(saved['policy'])
    _finite_tensors(saved['optimizer'])
    with torch.random.fork_rng(devices=[]):
        agent = RecurrentPPO(**saved['architecture'])
        if agent.policy.encoder[0].in_features != len(feature_names):
            raise ValueError('Architecture/schema mismatch')
        agent.policy.load_state_dict(saved['policy'], strict=True)
        agent.optimizer.load_state_dict(saved['optimizer'])
        agent.version = saved['update_version']
        executions = [_restore_execution(e) for e in saved['executions']]
        if not executions or len(executions) > agent.max_symbols or len({e.symbol for e in executions}) != len(executions):
            raise ValueError('Invalid saved roster')
        peaks = saved['peak_equity']
        if peaks.shape != (len(executions),) or not torch.isfinite(peaks).all() or (peaks <= 0).any():
            raise ValueError('Invalid reward peak state')
        if any(float(p) + 1e-4 < max(e.equity, e.config.initial_cash) for p, e in zip(peaks, executions)):
            raise ValueError('Reward peak below account equity')
        archived = saved['inactive_accounts']
        if not isinstance(archived, dict) or len(archived) > 5000:
            raise ValueError('Invalid inactive account archive')
        inactive = {}
        for symbol, item in archived.items():
            execution = _restore_execution(item['execution'])
            old_peak = item['peak']
            if (symbol != execution.symbol or execution.quantity or symbol in {e.symbol for e in executions}
                    or isinstance(old_peak, bool) or not isinstance(old_peak, (int, float))
                    or not math.isfinite(old_peak) or old_peak < max(execution.equity, execution.config.initial_cash)):
                raise ValueError('Invalid inactive account')
            inactive[symbol] = dict(execution=execution, peak=old_peak)
        torch.set_rng_state(saved['rng'])
    torch.set_rng_state(saved['rng'])
    return dict(agent=agent, executions=executions, peak_equity=peaks, inactive_accounts=inactive,
        reward_config=RewardConfig(**saved['reward_config']), rollout_restored=False,
        status='waiting_for_fresh_market', broker_calls=0)
