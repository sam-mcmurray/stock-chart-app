"""Bounded current Scanner discovery, not historical feature-store scanning."""
import json
import math

from drl_agent.live_inputs import scanner_admission
from drl_agent.watch_replay import WatchConfig

BUCKETS = ('actionable', 'priority', 'lower_volume', 'higher_float')


def scanner_rows(client):
    rows = {}
    # SCAN count is a hint; only the first 500 decoded rows are retained.
    for bucket in BUCKETS:
        cursor = 0
        for _ in range(5):
            cursor, page = client.hscan('scanner:watchlist:' + bucket, cursor=cursor, count=100)
            for raw in page.values():
                try:
                    row = json.loads(raw)
                    symbol = row['symbol']
                    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
                        continue
                    stamp = row['ts']
                    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                        continue
                    if symbol not in rows or stamp > rows[symbol]['ts']:
                        rows[symbol] = row
                except (KeyError, TypeError, ValueError):
                    continue
                if len(rows) >= 500:
                    return list(rows.values())
            if not cursor:
                break
    return list(rows.values())


def load_records(connection, rows):
    ids = sorted({row['shadow_feature_id'] for row in rows
                  if isinstance(row.get('shadow_feature_id'), str) and row['shadow_feature_id']})
    if len(ids) > 500:
        raise ValueError('Feature lookup exceeds bound')
    if not ids:
        return {}
    records = connection.execute('SELECT id, symbol, ts, source, payload, created_at FROM feature_store '
        "WHERE id = ANY(%s) AND source = 'scanner_shadow'", (ids,)).fetchall()
    return {row['id']: dict(row) for row in records}


def watch_config(settings, *, capacity=50):
    # Missing saved settings are not replaced with an invented trading range.
    return WatchConfig(minimum_price=float(settings['price_min']), maximum_price=float(settings['price_max']),
        minimum_rvol=float(settings['rvol_threshold']), maximum_float=float(settings['float_max']),
        capacity=min(50, capacity), watch_window_seconds=604800)


def select_candidates(rows, records, config, *, timestamp, held_symbols=()):
    held = tuple(sorted(set(held_symbols)))
    if len(held) > config.capacity:
        raise ValueError('Held symbols exceed capacity; cannot drop position management')
    candidates, rejected = {}, {}
    for row in rows:
        symbol = row['symbol']
        record = records.get(row.get('shadow_feature_id'))
        decision = scanner_admission(record, symbol=symbol, timestamp=timestamp,
            current_price=row.get('last'), config=config)
        if not decision['watch_allowed']:
            rejected[symbol] = decision['reasons']
            continue
        candidate = decision['candidate']
        rank = (decision['entry_inputs_ready'], candidate['rvol'] or 0, candidate['snapshot_ts'])
        previous = candidates.get(symbol)
        if previous is None or rank > previous[0]:
            candidates[symbol] = (rank, record, decision)
    ranked = sorted(candidates, key=lambda s: (*(-float(v) for v in candidates[s][0]), s))
    selected = list(held)
    selected.extend(s for s in ranked if s not in held)
    selected = selected[:config.capacity]
    return dict(symbols=selected,
        records={s: candidates[s][1] for s in selected if s in candidates},
        admission={s: candidates[s][2] for s in selected if s in candidates},
        rejected=rejected, held_without_candidate=[s for s in held if s not in candidates])
