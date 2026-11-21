import { describe, expect, it } from 'vitest';
import {
  aggregateCandles,
  applyLiveQuoteToCandles,
  calculateMacd,
  calculateVwap,
  fillIntradayCandleGaps,
  normalizeChartCandles,
} from '../lib/chartIndicators';

describe('chart indicators', () => {
  const candles = [
    { datetime: 3_600, open: 10, high: 12, low: 9, close: 11, volume: 100 },
    { datetime: 5_400, open: 11, high: 13, low: 10, close: 12, volume: 200 },
    { datetime: 7_200, open: 12, high: 14, low: 11, close: 13, volume: 300 },
  ];

  it('normalizes, sorts, and deduplicates candles', () => {
    const result = normalizeChartCandles([candles[2], candles[0], candles[1], candles[1]]);
    expect(result.map((item) => item.time)).toEqual([3600, 5400, 7200]);
  });

  it('aggregates smaller bars into hourly OHLCV candles', () => {
    const result = aggregateCandles(candles, 3600);
    expect(result).toHaveLength(2);
    expect(result[0]).toMatchObject({ time: 3600, open: 10, high: 13, low: 9, close: 12, volume: 300 });
  });

  it('updates the active candle with a live quote and starts the next interval', () => {
    const active = applyLiveQuoteToCandles(candles.slice(0, 2), {
      last: 13.5,
      market_ts: 5_450,
    }, 60);
    expect(active.at(-1)).toMatchObject({
      time: 5_400,
      high: 13.5,
      close: 13.5,
    });

    const next = applyLiveQuoteToCandles(candles, {
      last: 14,
      market_ts: 7_260,
    }, 60);
    expect(next.at(-1)).toMatchObject({
      time: 7_260,
      open: 14,
      high: 14,
      low: 14,
      close: 14,
      volume: 0,
      live: true,
    });
  });

  it('fills same-session no-trade intervals with flat zero-volume candles', () => {
    const filled = fillIntradayCandleGaps([
      { time: 1_800_000_000, open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
      { time: 1_800_000_180, open: 10.7, high: 11.2, low: 10.6, close: 11, volume: 200 },
    ], 60);

    expect(filled).toHaveLength(4);
    expect(filled.slice(1, 3)).toEqual([
      { time: 1_800_000_060, open: 10.5, high: 10.5, low: 10.5, close: 10.5, volume: 0, noTrade: true },
      { time: 1_800_000_120, open: 10.5, high: 10.5, low: 10.5, close: 10.5, volume: 0, noTrade: true },
    ]);
  });

  it('calculates aligned VWAP and MACD series', () => {
    const normalized = normalizeChartCandles(candles);
    const vwap = calculateVwap(normalized);
    const macd = calculateMacd(normalized);

    expect(vwap).toHaveLength(3);
    expect(macd.macd).toHaveLength(3);
    expect(macd.signal).toHaveLength(3);
    expect(macd.histogram).toHaveLength(3);
    expect(vwap.every((point) => Number.isFinite(point.value))).toBe(true);
  });
});
