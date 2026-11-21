// Capture the real UI using synthetic responses only. No backend is contacted.
// Usage: node docs/capture-screenshots.mjs [path/to/playwright/index.mjs]
import { mkdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
const { chromium } = await import(process.argv[2] ? pathToFileURL(process.argv[2]).href : 'playwright');
const origin = 'https://127.0.0.1:3107';
const out = fileURLToPath(new URL('./images/', import.meta.url));
await mkdir(out, { recursive: true });
const now = Math.floor(Date.now() / 1000);
const settings = { price_min: 1.5, price_max: 20, rvol_threshold: 3, rvol_window_days: 90, float_max: 20000000, scan_interval: 60 };
const news = [
  ['Example company announces quarterly results', 'An illustrative earnings catalyst for the research workflow.', 'DEMO'],
  ['Technology sector: product launch watch', 'Synthetic coverage showing ticker links and source attribution.', 'SAMPLE'],
  ['Market calendar: upcoming economic releases', 'Example planning coverage. This is not a real news report.', 'DEMO'],
  ['Example manufacturer expands capacity', 'Fictional business development for documentation.', 'SAMPLE'],
].map(([title, summary, ticker], i) => ({ id: `demo-${i}`, title, summary, source: 'Demo News', provider: 'synthetic', tickers: [ticker], url: `https://example.com/demo-${i}`, published_ts: now - 600 * (i + 1) }));
const summary = { account: { equity: 25000, cash: 18000, day_pl: 125, unrealized_pl: 240, realized_pl: 315 }, price_buckets: [{ bucket: '$1-5', trades: 12, win_rate: 58, pl: 120, avg_pl: 10 }, { bucket: '$5-10', trades: 8, win_rate: 62.5, pl: 195, avg_pl: 24.375 }], recent_trades: [{ symbol: 'DEMO', qty: 50, entry: 8, exit: 8.5, pl: 25, bucket: '$5-10', close_ts: now - 86400 }], positions: [] };
const status = { ok: true, heartbeat: { status: 'ok', checked: 0, triggered: 0, market_data_source: 'schwab', result_count: 2, failed_count: 0 }, heartbeat_age_seconds: 2, recent_events: [], recent_execution_count: 0, execute_trades: false, monitor_enabled: true, focus_count: 2, recent_ticker_count: 2, recent_article_count: 4 };
const responses = {
  '/auth/status': { authenticated: true, seconds_to_expiry: 1500, seconds_to_reauthorization: 432000 },
  '/auth/trading': { auto_trader_enabled: false, has_trading_credentials: false },
  '/portfolio/summary': summary,
  '/portfolio/market_overview': { indices: [{ symbol: '$SPX', name: 'S&P 500 (demo)', last: 6000, change: 18, percent_change: 0.3 }, { symbol: '$DJI', name: 'Dow Jones (demo)', last: 42000, change: -42, percent_change: -0.1 }, { symbol: '$COMPX', name: 'NASDAQ (demo)', last: 19000, change: 95, percent_change: 0.5 }] },
  '/agent/stop-loss/status': status,
  '/agent/local-exit-readiness': { ready: false, reasons: ['Demo mode: live trading disabled'], database_available: true, protection_mode: 'local_trigger' },
  '/scanner/status': status, '/news/status': status,
  '/scanner/settings': settings,
  '/scanner/watchlists': { actionable: ['DEMO', 'SAMPLE'].map((symbol, i) => ({ symbol, last: 8.5 + i * 2, rvol: 4.2 - i, score: 8.5 - i, float: 12000000, macd_open: true, company: { name: `${symbol} Example Company`, float_shares: 12000000 }, news_sentiment: { label: 'positive', article_count: 2, assessed: true, method: 'keyword_rules' }, decision: { action: 'watch', confidence: 0.72, catalyst: { primary: 'earnings', direction: 'positive', sources: [] } } })), priority: [], higher_float: [], lower_volume: [], too_expensive: [] },
  '/scanner/decisions/history': [],
  '/scanner/schwab/quotes': { quotes: { DEMO: { symbol: 'DEMO', last: 8.5, fetched_ts: now, market_ts: now, realtime: false, day_change_pct: 2.4 } }, fetched_ts: now },
  '/scanner/context/DEMO': { symbol: 'DEMO', company: { name: 'Demo Company — synthetic data' }, articles: news },
  '/news/recent': news, '/news/events': [],
  '/calendar/state': { reviews: {}, planning_events: [], review_archives: [], updated_at: now },
  '/news/quality': { report: null, counts: { open: 0 }, issues: [] },
  '/rag/events/search': { collection_count: 0, hits: [] },
  '/news/llm/status': { available: true, enabled: false, version: 'demo', active_model: '', models: [], running_models: [], live_worker: {}, historical_backfill: {}, historical_helper: {}, historical_operations: {} },
  '/news/llm/watch': { phase: 'idle', inputs: [], outputs: {} },
  '/news/llm/quality': { summary: {}, samples: [], cache: {} },
  '/news/sentiment/status': { available: true, loaded: false, device: 'cpu' },
  '/news/sentiment/evaluations': { items: [] },
  '/news/sentiment/benchmark/cases': { items: [] },
  '/agent/evaluations/live-learning/status': { learner_status: 'not_deployed', capture_status: 'demo', symbol_count: 2, priority_symbols: 1, heartbeat_age_seconds: 2, market_observations: 1200, market_observation_queue_depth: 0, market_observation_errors: 0, market_observation_dropped: 0, book_observation_interval_ms: 10000, priority_observation_interval_ms: 1000, book_age_seconds: { NASDAQ_BOOK: 1, NYSE_BOOK: 2 } },
  '/agent/evaluations/training-data/status': { status: 'demo', stage: 'preparing_data', current_month: '2026-01', target_start: '2026-01-01', target_end_exclusive: '2026-02-01', completed_months: [], snapshot_ts: now, snapshot_age_seconds: 0, export: { status: 'complete', days: 31, articles: 500, symbols: 25 }, acquisition: { status: 'in_progress', completed: 200, remaining: 50, no_data: 0, symbol: 'DEMO' }, assembly: { status: 'pending', completed: 0, remaining: 250 } },
};
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 }, ignoreHTTPSErrors: true, serviceWorkers: 'block' });
  // WebSockets must never connect to the live account stream or Vite HMR.
  await context.routeWebSocket(/.*/, socket => socket.close());
  const unknown = new Set();
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin === origin) return route.continue();
    if (url.origin !== 'https://127.0.0.1:8443') return route.abort();
    let data = responses[url.pathname];
    if (url.pathname === '/portfolio/price_history') {
      const daily = url.searchParams.get('frequency_type') === 'daily';
      const step = daily ? 86400000 : Number(url.searchParams.get('frequency') || 5) * 60000;
      const end = Math.floor(Date.now() / step) * step;
      data = { symbol: 'DEMO', candles: Array.from({ length: 160 }, (_, i) => {
        const open = 7 + i * 0.009 + Math.sin(i * 0.35) * 0.18;
        const close = open + Math.sin(i * 1.7) * 0.08;
        return { datetime: end - (160 - i) * step, open, close, high: Math.max(open, close) + 0.1, low: Math.min(open, close) - 0.08, volume: 10000 + (i * 971) % 40000 };
      }) };
    }
    if (data === undefined) { unknown.add(url.pathname); return route.fulfill({ status: 501, contentType: 'application/json', body: JSON.stringify({ detail: 'No synthetic fixture' }) }); }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const [name, path] of Object.entries({ dashboard: '/dashboard', charts: '/charts/DEMO', scanner: '/scanner', calendar: '/calendar', news: '/news', 'news-llm': '/news-llm', research: '/research', 'research-training': '/research?tab=training-data', settings: '/settings', profile: '/profile' })) {
    await page.setViewportSize({ width: name === 'scanner' ? 2200 : 1600, height: name === 'charts' || name === 'calendar' ? 1350 : 1050 });
    await page.goto(origin + path);
    await page.waitForTimeout(1800);
    await page.evaluate(() => {
      const label = document.createElement('div');
      label.textContent = 'DOCUMENTATION PREVIEW • SYNTHETIC DATA • NO LIVE ACCOUNT';
      label.style.cssText = 'position:fixed;bottom:12px;right:16px;z-index:99999;padding:9px 14px;border-radius:6px;background:#142b40;color:#a6e3ff;font:12px system-ui;border:1px solid #4b849f;pointer-events:none';
      document.body.append(label);
    });
    await page.screenshot({ path: `${out}${name}.png`, fullPage: false });
    console.log(`Captured ${name}`);
  }
  console.log(JSON.stringify({ unknownEndpoints: [...unknown], pageErrors: errors }));
  if (unknown.size || errors.length) process.exitCode = 1;
} finally { await browser.close(); }
