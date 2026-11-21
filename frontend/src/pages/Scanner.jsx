import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { FiArrowDown, FiArrowUp, FiExternalLink, FiMaximize2, FiMinus, FiRefreshCw, FiSearch } from 'react-icons/fi';
import {
  getPriceHistory,
  getScannerDecisionHistory,
  getScannerSettings,
  getScannerStatus,
  getScannerWatchlists,
  getSchwabScannerDecision,
  getSchwabScannerQuotes,
  updateScannerSettings,
} from '../lib/api';
import { toNumericSettings, validateScannerSettings } from '../lib/scannerSettings';
import { applyLiveQuoteToCandles, fillIntradayCandleGaps } from '../lib/chartIndicators';
import CandleChart from '../components/CandleChart.jsx';

const fmtMoney = (n) =>
  (n ?? 0) >= 0 ? `$${(n||0).toLocaleString(undefined,{maximumFractionDigits:2})}`
                : `-$${Math.abs(n||0).toLocaleString(undefined,{maximumFractionDigits:2})}`;

function formatSentiment(sentiment) {
  if (!sentiment || sentiment.assessed === false || Number(sentiment.article_count || 0) === 0) {
    return 'Not assessed';
  }
  const label = sentiment.label
    ? `${sentiment.label.charAt(0).toUpperCase()}${sentiment.label.slice(1)}`
    : 'Unknown';
  return sentiment.method === 'keyword_rules' ? `${label} · rules` : label;
}

const ScannerTableSkeleton = () => (
  <table className="scanner-skeleton-table">
    <thead>
      <tr>
        <th>Security</th>
        <th>Live Price</th>
        <th>Avg Volume Ratio</th>
        <th>MACD Open</th>
        <th>Score</th>
      </tr>
    </thead>
    <tbody>
      {Array.from({ length: 5 }).map((_, i) => (
        <tr key={i}>
          <td><div className="scanner-skeleton-line scanner-skeleton-symbol"></div></td>
          <td><div className="scanner-skeleton-line scanner-skeleton-price"></div></td>
          <td><div className="scanner-skeleton-line scanner-skeleton-rvol"></div></td>
          <td><div className="scanner-skeleton-line scanner-skeleton-macd"></div></td>
          <td><div className="scanner-skeleton-line scanner-skeleton-score"></div></td>
        </tr>
      ))}
    </tbody>
  </table>
);

function currentUrlSymbol() {
  return new URLSearchParams(window.location.search).get('symbol')?.trim().toUpperCase() || '';
}

function setUrlSymbol(symbol) {
  const url = new URL(window.location.href);
  url.searchParams.set('symbol', symbol);
  window.history.pushState({}, '', url.toString());
}

export default function Scanner(){
  const [requestedSymbol, setRequestedSymbol] = useState(currentUrlSymbol);
  const detailRequestRef = useRef(0);
  const [watchlists, setWatchlists] = useState({});
  const [scannerStatus, setScannerStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [candles, setCandles] = useState([]);
  const [chartError, setChartError] = useState('');
  const [chartLoading, setChartLoading] = useState(false);
  const [history, setHistory] = useState([]);
  const [settings, setSettings] = useState(null);
  const [settingsDraft, setSettingsDraft] = useState({});
  const [settingsError, setSettingsError] = useState('');
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [liveQuotes, setLiveQuotes] = useState({});
  const [liveQuoteError, setLiveQuoteError] = useState('');
  const [liveQuoteUpdatedAt, setLiveQuoteUpdatedAt] = useState(null);

  const fetchScannerResults = useCallback(async ({ initial = false } = {}) => {
    try {
      if (initial) setLoading(true);
      else setRefreshing(true);
      setError(null);
      const [data, scannerSettings, workerStatus] = await Promise.all([
        getScannerWatchlists(),
        getScannerSettings().catch(() => null),
        getScannerStatus().catch(() => null),
      ]);
      setWatchlists(data);
      setScannerStatus(workerStatus);
      if (scannerSettings) {
        setSettings(scannerSettings);
        setSettingsDraft(scannerSettings);
      }
    } catch (err) {
      setError(err);
    } finally {
      if (initial) setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchScannerResults({ initial: true });
  }, [fetchScannerResults]);

  useEffect(() => {
    const onPopState = () => {
      setRequestedSymbol(currentUrlSymbol());
    };

    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  useEffect(() => {
    if (!autoRefresh || !settings?.scan_interval) return undefined;
    const intervalMs = Math.max(Number(settings.scan_interval) || 30, 5) * 1000;
    const timer = setInterval(() => {
      fetchScannerResults();
    }, intervalMs);
    return () => clearInterval(timer);
  }, [autoRefresh, fetchScannerResults, settings?.scan_interval]);

  const watchlistSymbols = useMemo(
    () => [...new Set(
      Object.values(watchlists)
        .flat()
        .map((row) => row?.symbol)
        .filter(Boolean),
    )].sort(),
    [watchlists],
  );
  useEffect(() => {
    if (!watchlistSymbols.length) {
      setLiveQuotes({});
      setLiveQuoteError('');
      setLiveQuoteUpdatedAt(null);
      return undefined;
    }

    let stopped = false;
    let requestActive = false;
    const refreshLiveQuotes = async () => {
      if (requestActive || document.visibilityState === 'hidden') return;
      requestActive = true;
      try {
        const result = await getSchwabScannerQuotes(watchlistSymbols);
        if (stopped) return;
        setLiveQuotes((current) => {
          const next = { ...current };
          Object.entries(result?.quotes || {}).forEach(([symbol, quote]) => {
            const previousLive = Number(current[symbol]?.last);
            const storedPrice = Object.values(watchlists)
              .flat()
              .find((row) => row?.symbol === symbol)?.last;
            const previous = Number.isFinite(previousLive) ? previousLive : Number(storedPrice);
            const last = Number(quote?.last);
            const tickDirection = Number.isFinite(previous) && Number.isFinite(last)
              ? last > previous ? 'up' : last < previous ? 'down' : current[symbol]?.tick_direction || 'flat'
              : current[symbol]?.tick_direction || 'flat';
            next[symbol] = { ...current[symbol], ...quote, tick_direction: tickDirection };
          });
          return next;
        });
        setLiveQuoteUpdatedAt(Number(result?.fetched_ts) || Math.floor(Date.now() / 1000));
        setLiveQuoteError('');
      } catch (quoteError) {
        if (!stopped) setLiveQuoteError(quoteError.message || 'Live Schwab quote refresh failed');
      } finally {
        requestActive = false;
      }
    };

    refreshLiveQuotes();
    const timer = setInterval(refreshLiveQuotes, 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [watchlistSymbols, watchlists]);

  const saveSettings = async (event) => {
    event.preventDefault();
    setSettingsError('');
    setSettingsSaving(true);
    try {
      const numericSettings = toNumericSettings(settingsDraft);
      const validationError = validateScannerSettings(numericSettings);
      if (validationError) {
        setSettingsError(validationError);
        return;
      }
      const nextSettings = await updateScannerSettings(numericSettings);
      setSettings(nextSettings);
      setSettingsDraft(nextSettings);
    } catch (err) {
      setSettingsError(err.message);
    } finally {
      setSettingsSaving(false);
    }
  };

  const openDetail = useCallback(async (symbol) => {
    const normalizedSymbol = symbol.trim().toUpperCase();

    if (!normalizedSymbol) return;

    if (currentUrlSymbol() !== normalizedSymbol) {
      setUrlSymbol(normalizedSymbol);
      setRequestedSymbol(normalizedSymbol);
    }

    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;

    setSelected(normalizedSymbol);
    setDetail(null);
    setCandles([]);
    setHistory([]);
    setDetailError('');
    setChartError('');
    setDetailLoading(true);
    setChartLoading(true);

    getSchwabScannerDecision(normalizedSymbol)
      .then((data) => {
        if (detailRequestRef.current !== requestId) return;
        setDetail(data);
      })
      .catch((err) => {
        if (detailRequestRef.current !== requestId) return;
        setDetailError(err.message);
      })
      .finally(() => {
        if (detailRequestRef.current !== requestId) return;
        setDetailLoading(false);
      });

    getScannerDecisionHistory({ symbol: normalizedSymbol, limit: 5 })
      .then((items) => {
        if (detailRequestRef.current !== requestId) return;
        setHistory(items);
      })
      .catch(() => {
        if (detailRequestRef.current !== requestId) return;
        setHistory([]);
      });

    getPriceHistory(normalizedSymbol, {
        periodType: 'day',
        period: 10,
        frequencyType: 'minute',
        frequency: 5,
      })
      .then((priceHistory) => {
        if (detailRequestRef.current !== requestId) return;
        setCandles(priceHistory?.candles || []);
      })
      .catch((err) => {
        if (detailRequestRef.current !== requestId) return;
        setChartError(err.message);
      })
      .finally(() => {
        if (detailRequestRef.current !== requestId) return;
        setChartLoading(false);
      });
  }, []);

  useEffect(() => {
    if (!requestedSymbol) {
      if (!selected) return;
      detailRequestRef.current += 1;
      setSelected(null);
      setDetail(null);
      setCandles([]);
      setHistory([]);
      setDetailError('');
      setChartError('');
      setDetailLoading(false);
      setChartLoading(false);
      return;
    }

    if (requestedSymbol === selected) return;
    openDetail(requestedSymbol);
  }, [openDetail, requestedSymbol, selected]);

  if (error) return <div className="panel">Error: {error.message}</div>;

  const hasWatchlistRows = Object.values(watchlists).some(rows => rows?.length);
  const heartbeat = scannerStatus?.heartbeat || {};
  const heartbeatError = heartbeat.status === 'error' ? heartbeat.error : '';

  return (
    <section className="panel">
      <h2 className="page-title"><FiSearch /> Scanner</h2>
      <p>News-driven low-float momentum watchlists.</p>
      {scannerStatus && (
        <div className={heartbeatError ? 'state-error' : 'scanner-worker-summary'} role="status">
          {heartbeatError
            ? `Scanner unavailable: ${heartbeatError}`
            : `Worker ${heartbeat.status || 'unknown'} • ${heartbeat.market_data_source || 'unknown source'} • ${heartbeat.result_count ?? 0} results • ${heartbeat.failed_count ?? 0} failed${heartbeat.news_lookback_days ? ` • ${heartbeat.quote_monitored_count ?? 0}/${heartbeat.discovery_count ?? heartbeat.candidate_count ?? 0} quoted • ${heartbeat.price_eligible_count ?? heartbeat.candidate_count ?? 0} in price range • ${heartbeat.ticker_count ?? 0} deep-scanned • full sweep ~${heartbeat.estimated_full_sweep_minutes ?? '?'}m` : ''}`}
        </div>
      )}
      {!!watchlistSymbols.length && (
        <div className={liveQuoteError ? 'scanner-live-status state-error' : 'scanner-live-status'} role="status">
          <span className={`status-dot ${liveQuoteError ? 'status-dot-off' : 'status-dot-on'}`} />
          {liveQuoteError
            ? `Live Schwab prices delayed: ${liveQuoteError}`
            : liveQuoteUpdatedAt
              ? `Live Schwab prices • ${Object.keys(liveQuotes).length}/${watchlistSymbols.length} symbols • updated ${new Date(liveQuoteUpdatedAt * 1000).toLocaleTimeString()}`
              : 'Connecting to live Schwab prices...'}
        </div>
      )}
      <div className="scanner-controls">
        <button
          type="button"
          onClick={() => fetchScannerResults()}
          disabled={loading || refreshing}
          className="scanner-button"
        >
          <FiRefreshCw /> {refreshing ? 'Refreshing...' : 'Refresh'}
        </button>
        <label className="scanner-toggle">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(event) => setAutoRefresh(event.target.checked)}
          />
          Auto refresh{settings?.scan_interval ? ` every ${settings.scan_interval}s` : ''}
        </label>
      </div>
      <ScannerSettingsPanel
        settings={settings}
        draft={settingsDraft}
        error={settingsError}
        saving={settingsSaving}
        onChange={setSettingsDraft}
        onSubmit={saveSettings}
      />

      {loading ? (
        <ScannerTableSkeleton />
      ) : (
        <div className="scanner-layout">
          <div className="scanner-watchlists">
            {hasWatchlistRows ? (
              [
                ['actionable', 'Actionable'],
                ['priority', 'Priority Watchlist'],
                ['higher_float', 'Higher Float Watchlist'],
                ['lower_volume', 'Lower Volume Watchlist'],
                ['too_expensive', 'Price Watchlist'],
              ].map(([key, title]) => (
                <WatchlistTable
                  key={key}
                  title={title}
                  rows={watchlists[key] || []}
                  liveQuotes={liveQuotes}
                  onSelect={openDetail}
                />
              ))
            ) : (
              <div className="empty-panel">
                {heartbeatError
                  ? `No scan results: ${heartbeatError}`
                  : 'No scan results available. Check the worker status above.'}
              </div>
            )}
          </div>
          <DecisionPanel
            symbol={selected}
            detail={detail}
            loading={detailLoading}
            error={detailError}
            candles={candles}
            chartLoading={chartLoading}
            chartError={chartError}
            history={history}
            liveQuote={selected ? liveQuotes[selected] : null}
          />
        </div>
      )}
    </section>
  );
}

function ScannerSettingsPanel({ settings, draft, error, saving, onChange, onSubmit }){
  if (!settings) {
    return null;
  }

  const fields = [
    ['price_min', 'Min price', '0.01'],
    ['price_max', 'Max price', '0.01'],
    ['rvol_threshold', 'RVOL', '0.1'],
    ['rvol_window_days', 'RVOL days', '1'],
    ['float_max', 'Max float', '1'],
    ['scan_interval', 'Interval sec', '1'],
  ];

  return (
    <form onSubmit={onSubmit} className="scanner-settings">
      <div className="scanner-settings-grid">
        {fields.map(([key, label, step]) => (
          <label key={key} className="scanner-field">
            {label}
            <input
              type="number"
              step={step}
              value={draft[key] ?? ''}
              onChange={(event) => onChange({...draft, [key]: event.target.value})}
            />
          </label>
        ))}
      </div>
      <div className="scanner-settings-actions">
        <button type="submit" disabled={saving} className="scanner-save-button">
          {saving ? 'Saving...' : 'Save scanner settings'}
        </button>
        {error && <span className="scanner-settings-error">{error}</span>}
      </div>
    </form>
  );
}

function WatchlistTable({ title, rows, liveQuotes, onSelect }){
  if (!rows.length) return null;
  return (
    <div>
      <h3 className="page-title compact-title">{title}</h3>
      <div className="table-scroll">
        <table className="scanner-table">
          <thead>
            <tr>
              <th>Security</th>
              <th>Live Price</th>
              <th>Day Move</th>
              <th>RVOL</th>
              <th>Float</th>
              <th>MACD</th>
              <th>Decision</th>
              <th>Catalyst</th>
              <th>Market</th>
              <th>Sentiment</th>
              <th>News / Move</th>
              <th>Plan</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item, index) => (
              <tr key={`${item.symbol}-${index}`}>
                <td>
                  <button
                    onClick={() => onSelect(item.symbol)}
                    className="ticker-button"
                  >
                    {item.symbol}
                  </button>
                  <div className="scanner-company-name">
                    {item.company?.name || liveQuotes[item.symbol]?.description || 'Company unavailable'}
                  </div>
                  {!!item.watch_sources?.length && (
                    <div className="scanner-company-name">
                      {item.watch_sources.map((source) => source.replaceAll('_', ' ')).join(' • ')}
                    </div>
                  )}
                </td>
                <td>
                  <LivePrice quote={liveQuotes[item.symbol]} fallback={item.last} />
                </td>
                <td><DayMove quote={liveQuotes[item.symbol]} fallback={item.universe_quote} /></td>
                <td>{item.rvol ? `${item.rvol.toFixed(2)}x` : 'N/A'}</td>
                <td>{item.company?.float_shares ? item.company.float_shares.toLocaleString() : 'N/A'}</td>
                <td>{item.macd_open === undefined ? 'N/A' : item.macd_open ? 'Open' : 'Closed'}</td>
                <td>{item.decision ? `${item.decision.action} (${Math.round((item.decision.confidence || 0) * 100)}%)` : 'N/A'}</td>
                <td>
                  {item.decision?.catalyst ? `${item.decision.catalyst.primary} / ${item.decision.catalyst.direction}` : 'N/A'}
                  <CatalystSources
                    sources={item.decision?.catalyst?.sources || item.watch_source_details}
                    rawSources={item.watch_sources}
                  />
                </td>
                <td>{formatMarket(item.decision?.intraday)}</td>
                <td>{formatSentiment(item.news_sentiment)}</td>
                <td>{formatNewsCount(item.articles, item.universe_quote)}</td>
                <td>
                  {item.trade_plan
                    ? `${fmtMoney(item.trade_plan.stop_loss)} / ${fmtMoney(item.trade_plan.take_profit)}`
                    : item.reason || 'N/A'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DecisionPanel({ symbol, detail, loading, error, candles, chartLoading, chartError, history, liveQuote }){
  if (!symbol) {
    return (
      <aside className="panel decision-panel">
        <h3 className="page-title compact-title">Decision</h3>
        <p>Select a ticker for Schwab-backed quote, intraday, catalyst, and trade-plan details.</p>
      </aside>
    );
  }

  return (
    <aside className="panel decision-panel decision-panel-sticky">
      <div className="decision-panel-header">
        <div>
          <h3 className="page-title compact-title">{symbol} Decision</h3>
          <div className="decision-company-name">{detail?.company?.name || liveQuote?.description || 'Company unavailable'}</div>
        </div>
        <Link className="chart-workspace-link" to={`/charts/${encodeURIComponent(symbol)}`}>
          <FiMaximize2 /> Open charts
        </Link>
      </div>
      {loading && <p>Loading Schwab data...</p>}
      {error && <p className="state-error">{error}</p>}
      {chartLoading && <p>Loading candles...</p>}
      {chartError && <p className="state-error">{chartError}</p>}
      {!chartLoading && !chartError && (
        <Section title="Price">
          <CandleChart
            candles={fillIntradayCandleGaps(
              applyLiveQuoteToCandles(candles, liveQuote, 5 * 60),
              5 * 60,
            )}
            height={260}
            newsEvents={detail?.articles || []}
          />
        </Section>
      )}
      {detail && (
        <div className="decision-detail">
          <MetricGrid rows={[
            ['Action', detail.action],
            ['Confidence', `${Math.round((detail.confidence || 0) * 100)}%`],
            ['Source', detail.data_source || 'scanner'],
            ['Company', detail.company?.name || liveQuote?.description || 'N/A'],
            ['Exchange', detail.company?.exchange || 'N/A'],
            ['Live price', <LivePrice quote={liveQuote} fallback={detail.technicals?.last} />],
            ['Day move', <DayMove quote={liveQuote} />],
            ['Bid / Ask', liveQuote?.bid && liveQuote?.ask ? `${fmtMoney(liveQuote.bid)} / ${fmtMoney(liveQuote.ask)}` : 'N/A'],
            ['Mark', liveQuote?.mark ? fmtMoney(liveQuote.mark) : 'N/A'],
            ['RVOL', `${detail.technicals?.rvol ?? 'N/A'}x`],
            ['Float', detail.company?.float_shares ? detail.company.float_shares.toLocaleString() : 'N/A'],
            ['Market cap', detail.company?.market_cap ? fmtCompactNumber(detail.company.market_cap) : 'N/A'],
            ['News watch', formatNewsWatch(detail.news_watch)],
          ]} />
          <Section title="Related News">
            <NewsReleaseList articles={detail.articles || []} />
          </Section>
          <Section title="Catalyst">
            <div>{detail.catalyst?.primary || 'unknown'} / {detail.catalyst?.direction || 'neutral'}</div>
            <CatalystSources
              sources={detail.catalyst?.sources || detail.watch_source_details}
              rawSources={detail.watch_sources}
            />
            <SmallList rows={(detail.catalyst?.matches || []).map(m => `${m.type}: ${(m.terms || []).join(', ')}`)} />
          </Section>
          {detail.financial_analysis && <Section title="Financial Analysis">
            <MetricGrid rows={[
              ['Method', detail.financial_analysis.source === 'llm' ? 'LLM' : 'Rules fallback'],
              ['Sentiment', detail.financial_analysis.sentiment || 'neutral'],
              ['Score', detail.financial_analysis.score ?? 'N/A'],
              ['Confidence', `${Math.round((detail.financial_analysis.confidence || 0) * 100)}%`],
              ['Catalyst', detail.financial_analysis.catalyst || 'unknown'],
              ['Articles', detail.financial_analysis.article_count || 0],
            ]} />
            <SmallList rows={(detail.financial_analysis.blocking_risks || []).map(item => `Risk: ${item}`)} fallback="No blocking article risks." />
            <SmallList rows={(detail.financial_analysis.evidence || []).map(item => `Evidence: ${item}`)} fallback="No supporting evidence extracted." />
          </Section>}
          <Section title="Market">
            <MetricGrid rows={[
              ['Session', detail.intraday?.session?.state || 'N/A'],
              ['Gap', pct(detail.intraday?.gap_pct)],
              ['VWAP', detail.intraday?.vwap ? fmtMoney(detail.intraday.vwap) : 'N/A'],
              ['Above VWAP', boolText(detail.intraday?.above_vwap)],
              ['Volume pace', `${detail.intraday?.volume_pace ?? 'N/A'}x`],
              ['Halt risk', boolText(detail.intraday?.halt_risk)],
            ]} />
          </Section>
          <Section title="Trade Plan">
            <MetricGrid rows={[
              ['Entry', fmtMoney(detail.trade_plan?.entry_reference)],
              ['Stop', fmtMoney(detail.trade_plan?.stop_loss)],
              ['Target', fmtMoney(detail.trade_plan?.take_profit)],
              ['Risk/share', fmtMoney(detail.trade_plan?.risk_per_share)],
              ['R/R', detail.trade_plan?.risk_reward ?? 'N/A'],
            ]} />
          </Section>
          <Section title="Reasons">
            <SmallList rows={detail.reasons || []} fallback="No blocking reasons." />
          </Section>
          <Section title="Recent Decisions">
            <SmallList
              rows={(history || []).map(item => `${formatTs(item.ts)} ${item.action || 'N/A'} ${Math.round((item.confidence || 0) * 100)}% ${item.source || ''}`)}
              fallback="No history yet."
            />
          </Section>
        </div>
      )}
    </aside>
  );
}

function Section({ title, children }){
  return (
    <div className="decision-section">
      <div className="decision-section-title">{title}</div>
      {children}
    </div>
  );
}

function MetricGrid({ rows }){
  return (
    <div className="metric-grid">
      {rows.map(([label, value]) => (
        <div key={label} className="metric-cell">
          <div className="metric-label">{label}</div>
          <div className="metric-value">{value ?? 'N/A'}</div>
        </div>
      ))}
    </div>
  );
}

function LivePrice({ quote, fallback }){
  const direction = quote?.tick_direction || 'flat';
  return (
    <span
      className={`scanner-live-price movement-${direction}`}
      aria-label={`Live price ${direction}`}
      title={direction === 'up' ? 'Last price moved up' : direction === 'down' ? 'Last price moved down' : 'No observed tick change'}
    >
      {fmtMoney(quote?.last ?? fallback)}
    </span>
  );
}

function DayMove({ quote, fallback }){
  const value = numericOrNull(quote?.day_change_pct ?? fallback?.day_change_pct);
  if (value === null) return <span className="scanner-day-move movement-flat"><FiMinus /> N/A</span>;
  const direction = value > 0 ? 'up' : value < 0 ? 'down' : 'flat';
  const Icon = direction === 'up' ? FiArrowUp : direction === 'down' ? FiArrowDown : FiMinus;
  return (
    <span className={`scanner-day-move movement-${direction}`} aria-label={`Day move ${direction}`}>
      <Icon aria-hidden="true" /> {value > 0 ? '+' : ''}{value.toFixed(2)}%
    </span>
  );
}

function numericOrNull(value){
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function SmallList({ rows, fallback = 'None' }){
  if (!rows.length) return <div className="muted-list-empty">{fallback}</div>;
  return (
    <ul className="small-list">
      {rows.map((row, index) => <li key={index}>{row}</li>)}
    </ul>
  );
}

function CatalystSources({ sources, rawSources }){
  const labels = sources?.length
    ? sources.map((source) => source?.label || formatWatchSource(source?.type || source))
    : (rawSources || []).map(formatWatchSource);
  if (!labels.length) return <div className="scanner-company-name">Source unavailable</div>;
  return <div className="scanner-company-name">Source: {labels.join(' • ')}</div>;
}

function formatWatchSource(source){
  const labels = {
    recent_news: 'Verified recent company news',
    calendar_catalyst: 'Calendar or earnings catalyst',
    schwab_mover: 'Schwab market movers',
    direct_lookup: 'Direct scanner lookup',
  };
  if (labels[source]) return labels[source];
  if (source?.startsWith('graph_')) {
    return `Company relationship: ${source.slice(6).replaceAll('_', ' ')}`;
  }
  return String(source || '').replaceAll('_', ' ');
}

function NewsReleaseList({ articles }){
  if (!articles.length) return <div className="muted-list-empty">No verified related articles.</div>;
  return (
    <div className="scanner-news-list">
      {articles.map((article, index) => {
        const href = safeArticleUrl(article);
        return (
          <article className="scanner-news-item" key={article.id || href || `${article.title}-${index}`}>
            <div className="scanner-news-meta">
              <span>{formatTs(article.published_ts)}</span>
              <span>{article.source || article.provider || article.relevance_label || 'News'}</span>
              {article.article_verification_status && <span>{article.article_verification_status}</span>}
              {article.coverage_sequence && (
                <span className={`scanner-news-sequence scanner-news-sequence-${article.coverage_sequence}`}>
                  {article.coverage_sequence === 'earliest_in_lookback' ? 'Earliest found this week' : 'Follow-up coverage'}
                </span>
              )}
            </div>
            {href ? (
              <a href={href} target="_blank" rel="noreferrer" className="scanner-news-title">
                {article.title || 'Open article'} <FiExternalLink aria-hidden="true" />
              </a>
            ) : (
              <div className="scanner-news-title">{article.title || 'Untitled article'}</div>
            )}
            {article.summary && <p>{article.summary}</p>}
          </article>
        );
      })}
    </div>
  );
}

function formatMarket(intraday){
  if (!intraday) return 'N/A';
  const state = intraday.session?.state || 'unknown';
  const gap = Number.isFinite(intraday.gap_pct) ? `${intraday.gap_pct.toFixed(1)}%` : 'N/A';
  const vwap = intraday.above_vwap === null || intraday.above_vwap === undefined ? '' : intraday.above_vwap ? ' VWAP+' : ' VWAP-';
  const halt = intraday.halt_risk ? ' halt-risk' : '';
  const source = intraday.source === 'intraday' ? ' live' : '';
  return `${state}${source} gap ${gap}${vwap}${halt}`;
}

function pct(value){
  return Number.isFinite(value) ? `${value.toFixed(1)}%` : 'N/A';
}

function boolText(value){
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  return 'N/A';
}

function formatTs(ts){
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
  } catch {
    return '';
  }
}

function formatNewsCount(articles, quote){
  if (!articles?.length) return 'None';
  const latest = Math.max(...articles.map((article) => Number(article.published_ts) || 0));
  const count = Math.max(...articles.map((article) => Number(article.coverage_count) || 0), articles.length);
  const intervalMove = Number(quote?.interval_change_pct);
  const dayMove = Number(quote?.day_change_pct);
  const move = Number.isFinite(intervalMove) && Math.abs(intervalMove) >= 0.01
    ? ` • ${intervalMove >= 0 ? '+' : ''}${intervalMove.toFixed(2)}% cycle`
    : Number.isFinite(dayMove) && Math.abs(dayMove) >= 0.01
      ? ` • ${dayMove >= 0 ? '+' : ''}${dayMove.toFixed(2)}% today`
      : '';
  return `${count} • latest ${formatRelativeAge(latest)}${move}`;
}

function formatRelativeAge(ts){
  if (!ts) return 'time unavailable';
  const seconds = Math.max(0, Math.floor(Date.now() / 1000) - Number(ts));
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

function formatNewsWatch(watch){
  if (!watch || watch.state === 'none') return 'No verified news';
  const label = watch.state === 'fresh' ? 'Fresh catalyst watch' : 'Active catalyst watch';
  return `${label} • latest ${formatRelativeAge(watch.latest_published_ts)}`;
}

function fmtCompactNumber(value){
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 'N/A';
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 2,
  }).format(numeric);
}

function safeArticleUrl(article){
  const value = article?.resolved_article_url || article?.article_url || article?.url || article?.supporting_url;
  if (!value) return '';
  try {
    const parsed = new URL(value);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.toString() : '';
  } catch {
    return '';
  }
}
