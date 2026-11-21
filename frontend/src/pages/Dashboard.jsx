import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useInRouterContext } from 'react-router-dom';
import {
  buildWebSocketUrl,
  getDashboardSummary,
  getMarketOverview,
  getStopLossStatus,
  getTradingStatus,
  updateTradingStatus,
} from '../lib/api';
import BucketHistogram from '../components/BucketHistogram.jsx';
import {
  FiArrowUpRight,
  FiArrowDownRight,
  FiDollarSign,
  FiRefreshCw,
} from 'react-icons/fi';
import useWebSocket from '../hooks/useWebSocket.jsx';

const fmtMoney = (n) =>
  (n ?? 0) >= 0
    ? `$${(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
    : `-$${Math.abs(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

const fmtAge = (seconds) => {
  if (seconds === null || seconds === undefined) return 'no heartbeat';
  if (seconds < 5) return 'fresh';
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m`;
};

const fmtNumber = (n) =>
  (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 4 });

const fmtIndex = (n) =>
  Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const positionQty = (position) =>
  Number(position.longQuantity || 0) - Number(position.shortQuantity || 0);

const positionUnrealizedPl = (position) => {
  const qty = positionQty(position);
  return (Number(position.marketPrice || 0) - Number(position.averagePrice || 0)) * qty;
};

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [autoTrader, setAutoTrader] = useState(false);
  const [tradingSaving, setTradingSaving] = useState(false);
  const [tradingErr, setTradingErr] = useState('');
  const [stopLossStatus, setStopLossStatus] = useState(null);
  const [marketIndices, setMarketIndices] = useState([]);
  const [marketErr, setMarketErr] = useState('');

  const wsUrl = useMemo(() => buildWebSocketUrl('/ws/acct'), []);
  const { lastMessage } = useWebSocket(wsUrl);

  const fetchDashboard = useCallback(async ({ initial = false } = {}) => {
    try {
      if (initial) setLoading(true);
      else setRefreshing(true);

      setErr('');

      const [summaryResult, marketResult] = await Promise.allSettled([
        getDashboardSummary(),
        getMarketOverview(),
      ]);
      if (summaryResult.status === 'rejected') throw summaryResult.reason;
      const summary = summaryResult.value;

      if (!summary || typeof summary !== 'object') {
        throw new Error('Dashboard summary payload is empty or not an object');
      }

      if (!summary.account) {
        console.warn('[Dashboard] Missing account object in summary payload:', summary);
      }

      setData(summary);
      if (marketResult.status === 'fulfilled') {
        setMarketIndices(Array.isArray(marketResult.value?.indices) ? marketResult.value.indices : []);
        setMarketErr('');
      } else {
        setMarketErr(marketResult.reason?.message || String(marketResult.reason));
      }
    } catch (e) {
      console.error('[Dashboard] fetchDashboard failed:', e);
      setErr(e?.message || String(e));
    } finally {
      if (initial) setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const fetchTradingStatus = useCallback(async () => {
    try {
      setTradingErr('');
      const [status, stopLoss] = await Promise.all([
        getTradingStatus(),
        getStopLossStatus(),
      ]);
      setAutoTrader(Boolean(status?.auto_trader_enabled));
      setStopLossStatus(stopLoss);
    } catch (e) {
      console.error('[Dashboard] getTradingStatus failed:', e);
      setTradingErr(e?.message || String(e));
    }
  }, []);

  const setAutoTraderMode = useCallback(async (enabled) => {
    const previous = autoTrader;
    setAutoTrader(enabled);
    setTradingSaving(true);
    setTradingErr('');

    try {
      const status = await updateTradingStatus(enabled);
      setAutoTrader(Boolean(status?.auto_trader_enabled));
      fetchTradingStatus();
    } catch (e) {
      console.error('[Dashboard] updateTradingStatus failed:', e);
      setAutoTrader(previous);
      setTradingErr(e?.message || String(e));
    } finally {
      setTradingSaving(false);
    }
  }, [autoTrader, fetchTradingStatus]);

  useEffect(() => {
    fetchDashboard({ initial: true });
    fetchTradingStatus();
  }, [fetchDashboard, fetchTradingStatus]);

  useEffect(() => {
    if (!autoRefresh) return undefined;

    const timer = setInterval(() => {
      fetchDashboard();
    }, 60_000);

    return () => {
      clearInterval(timer);
    };
  }, [autoRefresh, fetchDashboard]);

  useEffect(() => {
    if (!lastMessage) return;

    if (lastMessage.service === 'ACCT_ACTIVITY') {
      const accountData = lastMessage.data;

      if (!accountData) {
        console.warn('[Dashboard] ACCT_ACTIVITY message has no data:', lastMessage);
        return;
      }

      setData((prevData) => {
        if (!prevData) {
          console.warn('[Dashboard] WS arrived before dashboard summary loaded');
          return prevData;
        }

        const prevAccount = prevData.account || {};
        const newAccount = { ...prevAccount };

        if (accountData.equity !== undefined) newAccount.equity = accountData.equity;
        if (accountData.cash !== undefined) newAccount.cash = accountData.cash;
        if (accountData.day_pl !== undefined) newAccount.day_pl = accountData.day_pl;

        const updated = {
          ...prevData,
          account: newAccount,
        };

        return updated;
      });
    }
  }, [lastMessage]);

  if (err && !data) {
    return <div className="panel">Failed to load: {err}</div>;
  }

  if (loading || !data) {
    return <div className="panel">Loading dashboard…</div>;
  }

  const {
    account = {},
    positions = [],
    price_buckets = [],
    recent_trades = [],
  } = data;

  if (!account || typeof account !== 'object') {
    console.error('[Dashboard] Invalid account object:', account);
    return <div className="panel">Dashboard payload is missing account data.</div>;
  }

  const workerHeartbeat = stopLossStatus?.heartbeat || {};
  const workerStatus = workerHeartbeat.status || workerHeartbeat.auto_trader || workerHeartbeat.monitor || 'unknown';
  const workerChecked = workerHeartbeat.checked ?? 0;
  const workerTriggered = workerHeartbeat.triggered ?? 0;
  const recentEventCount = Array.isArray(stopLossStatus?.recent_events)
    ? stopLossStatus.recent_events.length
    : 0;
  const recentExecutionCount = stopLossStatus?.recent_execution_count ?? 0;
  const heartbeatAge = fmtAge(stopLossStatus?.heartbeat_age_seconds);

  return (
    <section className="dashboard-grid">
      <div className="dashboard-controls">
        <button
          type="button"
          onClick={() => fetchDashboard()}
          disabled={refreshing}
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
          Auto refresh every 60s
        </label>

        <fieldset className="auto-trader-control" disabled={tradingSaving}>
          <legend>Auto Trader</legend>
          <label>
            <input
              type="radio"
              name="auto-trader"
              checked={autoTrader}
              onChange={() => setAutoTraderMode(true)}
            />
            On
          </label>
          <label>
            <input
              type="radio"
              name="auto-trader"
              checked={!autoTrader}
              onChange={() => setAutoTraderMode(false)}
            />
            Off
          </label>
        </fieldset>

        <div className="auto-trader-status" aria-label="Auto trader worker status">
          <span className={`status-dot ${autoTrader ? 'status-dot-on' : 'status-dot-off'}`} />
          <span>{autoTrader ? `Worker ${workerStatus}` : 'Worker idle'}</span>
          <span className="muted-small">
            {heartbeatAge} • {workerChecked} checked • {workerTriggered} triggered • {recentEventCount} stop events • {recentExecutionCount} executions
          </span>
        </div>

        {err && <span className="state-error">{err}</span>}
        {tradingErr && <span className="state-error">{tradingErr}</span>}
      </div>

      <KpiCard title="Equity" value={fmtMoney(account.equity)} icon={<FiDollarSign />} />
      <KpiCard title="Cash" value={fmtMoney(account.cash)} icon={<FiDollarSign />} />
      <KpiDelta title="Day P/L" value={account.day_pl} />
      <KpiDelta title="Unrealized P/L" value={account.unrealized_pl} />
      <KpiDelta title="Realized P/L (total)" value={account.realized_pl} />

      <section className="dashboard-full market-overview" aria-labelledby="market-overview-title">
        <div className="market-overview-heading">
          <h3 id="market-overview-title" className="page-title">Market Today</h3>
          {marketErr && <span className="state-error">Market overview unavailable: {marketErr}</span>}
        </div>
        <div className="market-index-grid">
          {marketIndices.map((index) => {
            const up = Number(index.change || 0) >= 0;
            return (
              <article className="market-index-card" key={index.symbol}>
                <div>
                  <h4>{index.name}</h4>
                  <span>{index.symbol}</span>
                </div>
                <strong>{fmtIndex(index.last)}</strong>
                <div className={up ? 'gain' : 'loss'}>
                  {up ? '+' : ''}{fmtIndex(index.change)} ({up ? '+' : ''}{fmtIndex(index.percent_change)}%)
                </div>
              </article>
            );
          })}
        </div>
      </section>

      <div className="panel dashboard-full">
        <h3 className="page-title">Open Positions</h3>

        {Array.isArray(positions) && positions.length ? (
          <div className="table-scroll">
            <table className="dashboard-table positions-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Qty</th>
                  <th>Avg</th>
                  <th>Last</th>
                  <th>Market Value</th>
                  <th>Day P/L</th>
                  <th>Unrealized P/L</th>
                </tr>
              </thead>

              <tbody>
                {positions.map((position, index) => {
                  const qty = positionQty(position);
                  const unrealizedPl = positionUnrealizedPl(position);

                  return (
                    <tr key={`${position.symbol || 'UNKNOWN'}-${index}`}>
                      <td className="symbol-cell">
                        <SymbolLink symbol={position.symbol} />
                      </td>
                      <td>{fmtNumber(qty)}</td>
                      <td>{fmtMoney(position.averagePrice)}</td>
                      <td>{fmtMoney(position.marketPrice)}</td>
                      <td>{fmtMoney(position.marketValue)}</td>
                      <td className={(position.currentDayProfitLoss || 0) >= 0 ? 'gain' : 'loss'}>
                        {fmtMoney(position.currentDayProfitLoss)}
                      </td>
                      <td className={unrealizedPl >= 0 ? 'gain' : 'loss'}>
                        {fmtMoney(unrealizedPl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-panel">No open positions.</div>
        )}
      </div>

      <div className="panel dashboard-wide">
        <h3 className="page-title">Performance by Entry Price</h3>

        {Array.isArray(price_buckets) && price_buckets.length ? (
          <BucketHistogram data={price_buckets} height={260} />
        ) : (
          <div className="empty-panel">No completed buy and sell pairs in the selected history.</div>
        )}

        <div className="bucket-grid">
          {(Array.isArray(price_buckets) ? price_buckets : []).map((b) => (
            <div key={b.bucket} className="bucket-card">
              <div className="bucket-title">{b.bucket}</div>
              <div className="muted-small">
                {b.trades} trades • Win {b.win_rate ?? 0}%
              </div>
              <div className="bucket-total">Total {fmtMoney(b.pl)}</div>
              <div className="muted-small">Avg {fmtMoney(b.avg_pl)}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="panel dashboard-wide">
        <h3 className="page-title">Recent Closed Trades</h3>

        {Array.isArray(recent_trades) && recent_trades.length ? <div className="table-scroll">
          <table className="dashboard-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>P/L</th>
                <th>Bucket</th>
              </tr>
            </thead>

            <tbody>
              {(Array.isArray(recent_trades) ? recent_trades : []).map((t, i) => (
                <tr key={i}>
                  <td className="symbol-cell">
                    <SymbolLink symbol={t.symbol} />
                  </td>
                  <td>{t.qty}</td>
                  <td>{t.entry}</td>
                  <td>{t.exit}</td>
                  <td className={t.pl >= 0 ? 'gain' : 'loss'}>{fmtMoney(t.pl)}</td>
                  <td className="muted-cell">{t.bucket}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div> : <div className="empty-panel">No closed trades found in the last 60 days.</div>}
      </div>
    </section>
  );
}

function SymbolLink({ symbol }) {
  const safeSymbol = symbol || 'UNKNOWN';
  const inRouter = useInRouterContext();

  if (!symbol) {
    return safeSymbol;
  }

  const to = `/scanner?symbol=${encodeURIComponent(symbol)}`;

  if (!inRouter) {
    return (
      <a className="symbol-link" href={to}>
        {safeSymbol}
      </a>
    );
  }

  return (
    <Link className="symbol-link" to={to}>
      {safeSymbol}
    </Link>
  );
}

function KpiCard({ title, value, icon }) {
  return (
    <div className="panel kpi-card">
      <div className="kpi-content">
        <div className="item-icon">{icon}</div>
        <div>
          <div className="muted-small">{title}</div>
          <div className="kpi-value">{value}</div>
        </div>
      </div>
    </div>
  );
}

function KpiDelta({ title, value }) {
  const up = (value || 0) >= 0;

  return (
    <div className="panel kpi-card">
      <div className="kpi-content">
        <div className="item-icon">
          {up ? <FiArrowUpRight /> : <FiArrowDownRight />}
        </div>
        <div>
          <div className="muted-small">{title}</div>
          <div className={`kpi-value ${up ? 'gain' : 'loss'}`}>
            {fmtMoney(value || 0)}
          </div>
        </div>
      </div>
    </div>
  );
}
