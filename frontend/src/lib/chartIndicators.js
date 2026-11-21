export function candleTime(datetime) {
  const numeric = Number(datetime);
  if (Number.isFinite(numeric)) {
    return numeric > 10_000_000_000 ? Math.floor(numeric / 1000) : Math.floor(numeric);
  }

  const parsed = Date.parse(datetime);
  return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : null;
}

export function normalizeChartCandles(candles) {
  const byTime = new Map();

  for (const candle of Array.isArray(candles) ? candles : []) {
    const time = candleTime(candle.datetime ?? candle.time);
    const open = Number(candle.open);
    const high = Number(candle.high);
    const low = Number(candle.low);
    const close = Number(candle.close);
    const volume = Number(candle.volume || 0);

    if (
      time === null ||
      !Number.isFinite(open) ||
      !Number.isFinite(high) ||
      !Number.isFinite(low) ||
      !Number.isFinite(close)
    ) {
      continue;
    }

    byTime.set(time, { time, open, high, low, close, volume });
  }

  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

function ema(values, period) {
  const multiplier = 2 / (period + 1);
  const output = [];
  let current = null;

  for (const value of values) {
    current = current === null ? value : value * multiplier + current * (1 - multiplier);
    output.push(current);
  }

  return output;
}

export function calculateMacd(candles) {
  if (!candles.length) return { macd: [], signal: [], histogram: [] };

  const closes = candles.map((candle) => candle.close);
  const fast = ema(closes, 12);
  const slow = ema(closes, 26);
  const macdValues = fast.map((value, index) => value - slow[index]);
  const signalValues = ema(macdValues, 9);

  return {
    macd: candles.map((candle, index) => ({ time: candle.time, value: macdValues[index] })),
    signal: candles.map((candle, index) => ({ time: candle.time, value: signalValues[index] })),
    histogram: candles.map((candle, index) => {
      const value = macdValues[index] - signalValues[index];
      return {
        time: candle.time,
        value,
        color: value >= 0 ? 'rgba(45,212,191,.55)' : 'rgba(248,113,113,.55)',
      };
    }),
  };
}

export function calculateVwap(candles, resetDaily = true) {
  let cumulativeVolume = 0;
  let cumulativeValue = 0;
  let activeDay = null;

  return candles.map((candle) => {
    const day = new Date(candle.time * 1000).toISOString().slice(0, 10);
    if (resetDaily && day !== activeDay) {
      activeDay = day;
      cumulativeVolume = 0;
      cumulativeValue = 0;
    }

    const typical = (candle.high + candle.low + candle.close) / 3;
    cumulativeVolume += candle.volume;
    cumulativeValue += typical * candle.volume;

    return {
      time: candle.time,
      value: cumulativeVolume > 0 ? cumulativeValue / cumulativeVolume : typical,
    };
  });
}

export function aggregateCandles(candles, intervalSeconds) {
  const normalized = normalizeChartCandles(candles);
  const buckets = new Map();

  for (const candle of normalized) {
    const time = Math.floor(candle.time / intervalSeconds) * intervalSeconds;
    const existing = buckets.get(time);
    if (!existing) {
      buckets.set(time, { ...candle, time });
      continue;
    }

    existing.high = Math.max(existing.high, candle.high);
    existing.low = Math.min(existing.low, candle.low);
    existing.close = candle.close;
    existing.volume += candle.volume;
  }

  return [...buckets.values()].sort((a, b) => a.time - b.time);
}

export function applyLiveQuoteToCandles(candles, quote, intervalSeconds) {
  const normalized = normalizeChartCandles(candles);
  const price = Number(quote?.last);
  const quoteTime = Number(quote?.market_ts || quote?.fetched_ts);
  if (!normalized.length || !Number.isFinite(price) || price <= 0 || !Number.isFinite(quoteTime)) {
    return normalized;
  }

  const next = normalized.map((candle) => ({ ...candle }));
  const last = next.at(-1);
  const isDaily = intervalSeconds >= 24 * 60 * 60;
  const quoteBucket = Math.floor(quoteTime / intervalSeconds) * intervalSeconds;
  const sameBar = isDaily
    ? quoteTime - last.time < 36 * 60 * 60
    : last.time === quoteBucket;

  if (sameBar) {
    last.high = Math.max(last.high, price);
    last.low = Math.min(last.low, price);
    last.close = price;
    return next;
  }

  if (quoteBucket > last.time) {
    next.push({
      time: quoteBucket,
      open: price,
      high: price,
      low: price,
      close: price,
      volume: 0,
      live: true,
    });
  }
  return next;
}

export function fillIntradayCandleGaps(candles, intervalSeconds) {
  const normalized = normalizeChartCandles(candles);
  if (normalized.length < 2 || intervalSeconds >= 24 * 60 * 60) return normalized;

  const marketDay = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
  const dayCache = new Map();
  const dayFor = (timestamp) => {
    if (!dayCache.has(timestamp)) {
      dayCache.set(timestamp, marketDay.format(new Date(timestamp * 1000)));
    }
    return dayCache.get(timestamp);
  };

  const filled = [normalized[0]];
  for (let index = 1; index < normalized.length; index += 1) {
    const previous = filled.at(-1);
    const current = normalized[index];
    if (dayFor(previous.time) === dayFor(current.time)) {
      for (let time = previous.time + intervalSeconds; time < current.time; time += intervalSeconds) {
        filled.push({
          time,
          open: previous.close,
          high: previous.close,
          low: previous.close,
          close: previous.close,
          volume: 0,
          noTrade: true,
        });
      }
    }
    filled.push(current);
  }
  return filled;
}
