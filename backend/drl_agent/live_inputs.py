"""Read-only batched Redis inputs and existing Scanner watch admission."""
from copy import deepcopy
import math
import os

from drl_agent.dataset import _normalize
from drl_agent.watch_replay import WatchConfig, _candidate
from services.market_stream_store import _loads, select_listing_book


class RedisLiveInputs:
    def __init__(self, client, symbols, *, candle_refresh_seconds=15):
        self.symbols = tuple(symbols)
        if (not 1 <= len(self.symbols) <= 50 or len(set(self.symbols)) != len(self.symbols)
                or not all(isinstance(s, str) and s and s == s.strip().upper() for s in self.symbols)
                or not 1 <= candle_refresh_seconds <= 60):
            raise ValueError('Invalid input reader configuration')
        self.client = client
        self.refresh = candle_refresh_seconds
        self.cache = {}
        self.keys = [os.getenv(name, default) for name, default in (
            ('SCHWAB_LEVELONE_HASH_KEY', 'schwab:stream:levelone'),
            ('SCHWAB_NASDAQ_BOOK_HASH_KEY', 'schwab:stream:nasdaq_book'),
            ('SCHWAB_NYSE_BOOK_HASH_KEY', 'schwab:stream:nyse_book'))]
        self.chart_prefix = os.getenv('SCHWAB_CHART_KEY_PREFIX', 'schwab:stream:chart:')

    def read(self, timestamp):
        if isinstance(timestamp, bool) or not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError('Invalid decision timestamp')
        minute = int(timestamp // 60)
        plan = []
        # Pipelining avoids one network round trip per symbol or data family.
        with self.client.pipeline(transaction=False) as pipe:
            for symbol in self.symbols:
                for key in self.keys:
                    pipe.hget(key, symbol)
                cached = self.cache.get(symbol)
                refresh = cached is None or cached['minute'] != minute or not 0 <= timestamp - cached['ts'] < self.refresh
                if refresh:
                    fields = [str((minute - n) * 60000) for n in range(120, 0, -1)]
                    pipe.hmget(self.chart_prefix + symbol, fields)
                plan.append((symbol, refresh))
            replies = iter(pipe.execute())
        snapshots, updates = {}, {}
        for symbol, refresh in plan:
            quote, nasdaq, nyse = [_loads(next(replies)) for _ in range(3)]
            if refresh:
                candles = [item for raw in next(replies) if (item := _loads(raw)) is not None]
                updates[symbol] = dict(ts=timestamp, minute=minute, candles=candles)
            else:
                candles = self.cache[symbol]['candles']
            snapshots[symbol] = dict(symbol=symbol, levelone=quote,
                book=select_listing_book(quote, nasdaq, nyse), candles=deepcopy(candles))
        self.cache.update(updates)
        return snapshots


def scanner_admission(record, *, symbol, timestamp, current_price, config: WatchConfig):
    """Watch admission is distinct from entry and cannot authorize broker orders."""
    result = dict(watch_allowed=False, entry_inputs_ready=False, reasons=[], broker_calls=0)
    try:
        if (record.get('symbol') != symbol or record.get('source') != 'scanner_shadow'
                or isinstance(timestamp, bool) or not math.isfinite(timestamp)
                or isinstance(current_price, bool) or not math.isfinite(current_price) or current_price <= 0):
            raise ValueError('Invalid scanner input')
        normalized = _normalize(record, scanner=True)
        if normalized['available_ms'] > timestamp * 1000:
            result['reasons'] = ['scanner_not_yet_available']
            return result
        candidate, reason = _candidate(normalized, timestamp, config)
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        result['reasons'] = ['invalid_scanner_input']
        return result
    if reason:
        result['reasons'] = [reason]
        return result
    if not config.minimum_price <= current_price <= config.maximum_price:
        result['reasons'] = ['current_price_out_of_range']
        return result
    result.update(watch_allowed=True, entry_inputs_ready=not candidate['entry_block_reasons'],
        reasons=candidate['entry_block_reasons'], candidate=candidate)
    return result
