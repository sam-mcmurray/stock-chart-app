import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { FiArrowDown, FiArrowLeft, FiArrowUp, FiExternalLink, FiMaximize2, FiMinimize2, FiMinus, FiRefreshCw, FiSearch } from 'react-icons/fi';
import CandleChart from '../components/CandleChart';
import { getPriceHistory, getScannerContext, getSchwabScannerQuotes } from '../lib/api';
import {
  aggregateCandles,
  applyLiveQuoteToCandles,
  fillIntradayCandleGaps,
} from '../lib/chartIndicators';

const TIMEFRAMES = [
  {
    key: 'minute',
    label: '1 Minute',
    intervalSeconds: 60,
    ranges: [
      { key: '1d', label: '1 Day', options: { periodType: 'day', period: 1, frequencyType: 'minute', frequency: 1 } },
      { key: '5d', label: '5 Days', options: { periodType: 'day', period: 5, frequencyType: 'minute', frequency: 1 } },
      { key: '10d', label: '10 Days', options: { periodType: 'day', period: 10, frequencyType: 'minute', frequency: 1 } },
      { key: '15d', label: '15 Days', options: { lookbackDays: 15, frequencyType: 'minute', frequency: 1 } },
      { key: '30d', label: '30 Days', options: { lookbackDays: 30, frequencyType: 'minute', frequency: 1 } },
    ],
  },
  {
    key: 'five-minute',
    label: '5 Minute',
    intervalSeconds: 5 * 60,
    ranges: [
      { key: '1d', label: '1 Day', options: { periodType: 'day', period: 1, frequencyType: 'minute', frequency: 5 } },
      { key: '5d', label: '5 Days', options: { periodType: 'day', period: 5, frequencyType: 'minute', frequency: 5 } },
      { key: '10d', label: '10 Days', options: { periodType: 'day', period: 10, frequencyType: 'minute', frequency: 5 } },
      { key: '30d', label: '30 Days', options: { lookbackDays: 30, frequencyType: 'minute', frequency: 5 } },
      { key: '60d', label: '60 Days', options: { lookbackDays: 60, frequencyType: 'minute', frequency: 5 } },
    ],
  },
  {
    key: 'hourly',
    label: 'Hourly',
    intervalSeconds: 60 * 60,
    ranges: [
      { key: '5d', label: '5 Days', options: { periodType: 'day', period: 5, frequencyType: 'minute', frequency: 30 } },
      { key: '10d', label: '10 Days', options: { periodType: 'day', period: 10, frequencyType: 'minute', frequency: 30 } },
      { key: '30d', label: '30 Days', options: { lookbackDays: 30, frequencyType: 'minute', frequency: 30 } },
      { key: '90d', label: '90 Days', options: { lookbackDays: 90, frequencyType: 'minute', frequency: 30 } },
      { key: '180d', label: '6 Months', options: { lookbackDays: 180, frequencyType: 'minute', frequency: 30 } },
      { key: '270d', label: 'Max (~9 Months)', options: { lookbackDays: 270, frequencyType: 'minute', frequency: 30 } },
    ],
    transform: (candles) => aggregateCandles(candles, 60 * 60),
  },
  {
    key: 'daily',
    label: 'Daily',
    intervalSeconds: 24 * 60 * 60,
    ranges: [
      { key: '1y', label: '1 Year', options: { periodType: 'year', period: 1, frequencyType: 'daily', frequency: 1 } },
      { key: '5y', label: '5 Years', options: { periodType: 'year', period: 5, frequencyType: 'daily', frequency: 1 } },
      { key: '10y', label: '10 Years', options: { periodType: 'year', period: 10, frequencyType: 'daily', frequency: 1 } },
    ],
    daily: true,
  },
];

const DEFAULT_RANGES = {
  minute: '10d',
  'five-minute': '10d',
  hourly: '10d',
  daily: '10y',
};

export default function ChartWorkspace() {
  const { symbol: routeSymbol = '' } = useParams();
  const navigate = useNavigate();
  const workspaceRef = useRef(null);
  const symbol = routeSymbol.trim().toUpperCase();
  const [draftSymbol, setDraftSymbol] = useState(symbol);
  const [charts, setCharts] = useState({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [ranges, setRanges] = useState(DEFAULT_RANGES);
  const [liveQuote, setLiveQuote] = useState(null);
  const [liveQuoteError, setLiveQuoteError] = useState('');
  const [scannerContext, setScannerContext] = useState({ company: {}, articles: [] });
  const rangesRef = useRef(DEFAULT_RANGES);
  const timeframeRequestRef = useRef({});

  useEffect(() => setDraftSymbol(symbol), [symbol]);

  useEffect(() => {
    let stopped = false;
    setScannerContext({ company: {}, articles: [] });
    if (!symbol) return undefined;
    getScannerContext(symbol)
      .then((result) => {
        if (!stopped) setScannerContext({
          company: result?.company || {},
          articles: result?.articles || [],
        });
      })
      .catch(() => {});
    return () => { stopped = true; };
  }, [symbol]);

  const loadTimeframe = useCallback(async (timeframe, rangeKey) => {
    if (!symbol) return;
    const requestId = (timeframeRequestRef.current[timeframe.key] || 0) + 1;
    timeframeRequestRef.current[timeframe.key] = requestId;
    const selectedRange = timeframe.ranges.find((range) => range.key === rangeKey) || timeframe.ranges.at(-1);

    try {
      const result = await getPriceHistory(symbol, selectedRange.options);
      if (timeframeRequestRef.current[timeframe.key] !== requestId) return;
      const rawCandles = result?.candles || [];
      setCharts((current) => ({
        ...current,
        [timeframe.key]: {
          candles: timeframe.transform ? timeframe.transform(rawCandles) : rawCandles,
          error: '',
        },
      }));
    } catch (error) {
      if (timeframeRequestRef.current[timeframe.key] !== requestId) return;
      setCharts((current) => ({
        ...current,
        [timeframe.key]: {
          candles: [],
          error: error?.message || String(error),
        },
      }));
    }
  }, [symbol]);

  const loadCharts = useCallback(async ({ initial = false } = {}) => {
    if (!symbol) return;
    if (initial) setLoading(true);
    else setRefreshing(true);

    await Promise.all(TIMEFRAMES.map(
      (timeframe) => loadTimeframe(timeframe, rangesRef.current[timeframe.key]),
    ));
    setLoading(false);
    setRefreshing(false);
  }, [symbol, loadTimeframe]);

  const changeRange = useCallback((timeframe, rangeKey) => {
    const nextRanges = { ...rangesRef.current, [timeframe.key]: rangeKey };
    rangesRef.current = nextRanges;
    setRanges(nextRanges);
    loadTimeframe(timeframe, rangeKey);
  }, [loadTimeframe]);

  useEffect(() => {
    loadCharts({ initial: true });
  }, [loadCharts]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = setInterval(() => loadCharts(), 60_000);
    return () => clearInterval(timer);
  }, [autoRefresh, loadCharts]);

  useEffect(() => {
    if (!symbol) return undefined;
    let stopped = false;
    let requestActive = false;

    const refreshLiveQuote = async () => {
      if (requestActive || document.visibilityState === 'hidden') return;
      requestActive = true;
      try {
        const result = await getSchwabScannerQuotes([symbol]);
        if (stopped) return;
        setLiveQuote((current) => {
          const quote = result?.quotes?.[symbol] || null;
          if (!quote) return null;
          const previous = Number(current?.last);
          const last = Number(quote.last);
          const tickDirection = Number.isFinite(previous) && Number.isFinite(last)
            ? last > previous ? 'up' : last < previous ? 'down' : current?.tick_direction || 'flat'
            : current?.tick_direction || 'flat';
          return { ...quote, tick_direction: tickDirection };
        });
        setLiveQuoteError(result?.quotes?.[symbol] ? '' : `No live Schwab quote returned for ${symbol}`);
      } catch (error) {
        if (!stopped) setLiveQuoteError(error?.message || 'Live Schwab quote refresh failed');
      } finally {
        requestActive = false;
      }
    };

    setLiveQuote(null);
    setLiveQuoteError('');
    refreshLiveQuote();
    const timer = setInterval(refreshLiveQuote, 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [symbol]);

  useEffect(() => {
    const onFullscreenChange = () => setIsFullscreen(document.fullscreenElement === workspaceRef.current);
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, []);

  const totalCandles = useMemo(
    () => Object.values(charts).reduce((sum, chart) => sum + (chart?.candles?.length || 0), 0),
    [charts],
  );

  const submitSymbol = (event) => {
    event.preventDefault();
    const next = draftSymbol.trim().toUpperCase();
    if (next) navigate(`/charts/${encodeURIComponent(next)}`);
  };

  const toggleFullscreen = async () => {
    if (!document.fullscreenElement) {
      await workspaceRef.current?.requestFullscreen?.();
    } else {
      await document.exitFullscreen?.();
    }
  };

  return (
    <section className="chart-workspace" ref={workspaceRef}>
      <header className="chart-workspace-header">
        <div className="chart-workspace-title">
          <Link to={`/scanner?symbol=${encodeURIComponent(symbol)}`} className="icon-link" aria-label="Back to scanner">
            <FiArrowLeft />
          </Link>
          <div>
            <h2>{symbol} Charts</h2>
            <div className="chart-company-name">{scannerContext.company?.name || liveQuote?.description || ''}</div>
            <p>{loading ? 'Loading Schwab history...' : `${totalCandles.toLocaleString()} bars across four timeframes`}</p>
            <div className={liveQuoteError ? 'chart-live-quote state-error' : 'chart-live-quote'}>
              <span className={`status-dot ${liveQuoteError ? 'status-dot-off' : 'status-dot-on'}`} />
              {liveQuoteError
                ? liveQuoteError
                : liveQuote
                  ? <>
                    Schwab live <span className={`chart-live-price movement-${liveQuote.tick_direction || 'flat'}`}>
                      {liveQuote.last.toLocaleString(undefined, { style: 'currency', currency: 'USD' })}
                    </span>
                    {liveQuote.day_change_pct !== null && liveQuote.day_change_pct !== undefined && Number.isFinite(Number(liveQuote.day_change_pct)) && (
                      <span className={`chart-day-move movement-${Number(liveQuote.day_change_pct) > 0 ? 'up' : Number(liveQuote.day_change_pct) < 0 ? 'down' : 'flat'}`}>
                        {' • '}{Number(liveQuote.day_change_pct) > 0 ? <FiArrowUp aria-hidden="true" /> : Number(liveQuote.day_change_pct) < 0 ? <FiArrowDown aria-hidden="true" /> : <FiMinus aria-hidden="true" />}{' '}{Number(liveQuote.day_change_pct) > 0 ? '+' : ''}{Number(liveQuote.day_change_pct).toFixed(2)}% today
                      </span>
                    )}
                    {' • '}updated {new Date((liveQuote.fetched_ts || 0) * 1000).toLocaleTimeString()}
                  </>
                  : 'Connecting to live Schwab quote...'}
            </div>
          </div>
        </div>

        <div className="chart-workspace-controls">
          <form className="chart-symbol-form" onSubmit={submitSymbol}>
            <label htmlFor="chart-symbol">Symbol</label>
            <div>
              <input
                id="chart-symbol"
                value={draftSymbol}
                onChange={(event) => setDraftSymbol(event.target.value)}
                maxLength={8}
              />
              <button type="submit" aria-label="Load symbol"><FiSearch /></button>
            </div>
          </form>

          <label className="scanner-toggle">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => setAutoRefresh(event.target.checked)}
            />
            Auto refresh 60s
          </label>

          <button
            type="button"
            className="chart-command"
            onClick={() => loadCharts()}
            disabled={refreshing}
          >
            <FiRefreshCw /> {refreshing ? 'Refreshing...' : 'Refresh'}
          </button>

          <button
            type="button"
            className="chart-icon-command"
            onClick={toggleFullscreen}
            aria-label={isFullscreen ? 'Exit full screen' : 'Enter full screen'}
            title={isFullscreen ? 'Exit full screen' : 'Enter full screen'}
          >
            {isFullscreen ? <FiMinimize2 /> : <FiMaximize2 />}
          </button>
        </div>
      </header>

      {!!scannerContext.articles.length && (
        <div className="chart-news-strip" aria-label="Related news releases">
          {scannerContext.articles.map((article, index) => {
            const href = safeArticleUrl(article);
            return (
              <a
                key={article.id || href || `${article.title}-${index}`}
                href={href || undefined}
                target={href ? '_blank' : undefined}
                rel={href ? 'noreferrer' : undefined}
                className={!href ? 'chart-news-link chart-news-link-disabled' : 'chart-news-link'}
              >
                <span>
                  {formatNewsTime(article.published_ts)} • {article.coverage_sequence === 'earliest_in_lookback' ? 'earliest found this week' : 'follow-up coverage'}
                </span>
                <strong>{article.title || 'Untitled article'}</strong>
                {href && <FiExternalLink aria-hidden="true" />}
              </a>
            );
          })}
        </div>
      )}

      <div className="multi-chart-grid">
        {TIMEFRAMES.map((timeframe) => {
          const chart = charts[timeframe.key] || { candles: [], error: '' };
          const selectedRange = timeframe.ranges.find((range) => range.key === ranges[timeframe.key]) || timeframe.ranges.at(-1);
          const displayedCandles = fillIntradayCandleGaps(
            applyLiveQuoteToCandles(chart.candles, liveQuote, timeframe.intervalSeconds),
            timeframe.intervalSeconds,
          );
          return (
            <article className="timeframe-chart" key={timeframe.key}>
              <div className="timeframe-chart-header">
                <div>
                  <h3>{timeframe.label}</h3>
                  <span>{timeframe.key === 'hourly' ? 'Aggregated from Schwab 30-minute bars' : selectedRange.label}</span>
                </div>
                <div className="timeframe-chart-meta">
                  <strong>{chart.candles.length.toLocaleString()} bars</strong>
                  <select
                    aria-label={`${timeframe.label} range`}
                    value={ranges[timeframe.key]}
                    onChange={(event) => changeRange(timeframe, event.target.value)}
                  >
                    {timeframe.ranges.map((range) => (
                      <option key={range.key} value={range.key}>{range.label}</option>
                    ))}
                  </select>
                </div>
              </div>
              {chart.error ? (
                <div className="chart-timeframe-error">{chart.error}</div>
              ) : loading ? (
                <div className="chart-loading">Loading...</div>
              ) : (
                <CandleChart
                  candles={displayedCandles}
                  height={420}
                  indicators
                  daily={timeframe.daily}
                  visibleBars={160}
                  newsEvents={scannerContext.articles}
                />
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function safeArticleUrl(article) {
  const value = article?.resolved_article_url || article?.article_url || article?.url || article?.supporting_url;
  if (!value) return '';
  try {
    const parsed = new URL(value);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.toString() : '';
  } catch {
    return '';
  }
}

function formatNewsTime(ts) {
  const numeric = Number(ts);
  if (!Number.isFinite(numeric) || numeric <= 0) return 'Time unavailable';
  return new Date(numeric * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}
