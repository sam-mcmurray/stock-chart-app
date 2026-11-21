// src/lib/api.js
export const API_BASE = import.meta.env.VITE_API_BASE || 'https://127.0.0.1:8443';

export function buildWebSocketUrl(path) {
  const url = new URL(path, API_BASE);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

/**
 * Small fetch wrapper with try/catch and JSON-safe parsing.
 * Always returns { ok, status, data, error }.
 */

function readCookie(name) {
  const raw = document.cookie.split('; ').find(c => c.startsWith(name + '='))?.split('=')[1];
  return raw ? decodeURIComponent(raw) : raw;
}

function csrfHeaders(headers = {}) {
  const next = { ...headers };
  const csrf = readCookie(import.meta.env.VITE_CSRF_COOKIE_NAME || 'csrf');
  if (csrf) next['X-CSRF-Token'] = csrf;
  return next;
}

async function safeBody(res) {
  const ct = res.headers.get('content-type') || '';
  try {
    return ct.includes('application/json') ? await res.json() : await res.text();
  } catch {
    return null;
  }
}

function errorText(body, fallback) {
  return (body && body.detail) || (typeof body === 'string' && body) || fallback;
}

async function throwFetchError(res, label) {
  const body = await safeBody(res);
  throw new Error(`${label} ${res.status}: ${String(errorText(body, 'HTTP error')).slice(0, 200)}`);
}

export async function api(path, opts = {}) {
  const url = `${API_BASE}${path}`;
  try {
    const method = opts.method || 'GET';
    const res = await fetch(url, {
      ...opts,
      credentials: 'include', // send HttpOnly session cookie
      headers: csrfHeaders({ 'Content-Type': 'application/json', ...(opts.headers || {}) }),
      method,
    });

    const body = await safeBody(res);

    if (!res.ok) {
      return {
        ok: false,
        status: res.status,
        data: body,
        error: errorText(body, `HTTP ${res.status}`),
      };
    }

    return { ok: true, status: res.status, data: body, error: null };
  } catch (err) {
    // Network / CORS / TLS errors end up here
    return {
      ok: false,
      status: 0,
      data: null,
      error: err?.message || 'Network error',
    };
  }
}

/** Central fetch (sends cookies; optionally adds CSRF header if you use it) */
export async function apiFetch(path, opts = {}) {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`;
  const headers = new Headers(opts.headers || {});
  const csrf = readCookie(import.meta.env.VITE_CSRF_COOKIE_NAME || 'csrf');
  if (csrf) headers.set('X-CSRF-Token', csrf);

  const res = await fetch(url, {
    method: opts.method || 'GET',
    credentials: 'include',
    headers,
    body: opts.body || undefined,
    signal: opts.signal,
    mode: 'cors',
  });

  if (!res.ok) {
    // Let callers handle 401/403/other
    const text = await res.text().catch(() => '');
    const err = new Error(`${res.status} ${res.statusText} :: ${text.slice(0,200)}`);
    err.status = res.status;
    throw err;
  }

  // Try json, fallback to text
  try { return await res.json(); } catch { return await res.text(); }
}

/** Build backend login URL for hard navigation */
export function buildLoginUrl(next = '/dashboard') {
  return `${API_BASE}/auth/login?next=${encodeURIComponent(next)}`;
}

/** Check auth status with try/catch wrapped result */
export async function getAuthStatus() {
  // -> { ok, status, data?: { authenticated, ... }, error? }
  return api('/auth/status');
}

/** Server-side logout (clears cookie + server tokens) with try/catch */
export async function logout() {
  return api('/auth/logout', { method: 'POST' });
}

export async function disconnectSchwab() {
  const result = await api('/auth/disconnect', { method: 'POST' });
  if (!result.ok) {
    throw new Error(`disconnectSchwab ${result.status}: ${result.error || 'Unable to disconnect Schwab'}`);
  }
  return result.data;
}

export async function getTradingStatus() {
  const res = await fetch(`${API_BASE}/auth/trading`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getTradingStatus');
  }
  return res.json();
}

export async function updateTradingStatus(autoTraderEnabled) {
  const res = await fetch(`${API_BASE}/auth/trading`, {
    method: 'PUT',
    credentials: 'include',
    headers: csrfHeaders({ 'Content-Type': 'application/json', 'Accept': 'application/json' }),
    body: JSON.stringify({ auto_trader_enabled: Boolean(autoTraderEnabled) }),
  });
  if (!res.ok) {
    await throwFetchError(res, 'updateTradingStatus');
  }
  return res.json();
}

export async function getStopLossStatus() {
  const res = await fetch(`${API_BASE}/agent/stop-loss/status`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getStopLossStatus');
  }
  return res.json();
}

export async function getLocalExitReadiness() {
  const res = await fetch(`${API_BASE}/agent/local-exit-readiness`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getLocalExitReadiness');
  }
  return res.json();
}

export async function getScannerStatus() {
  const res = await fetch(`${API_BASE}/scanner/status`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getScannerStatus');
  }
  return res.json();
}

export async function getNewsStatus() {
  const res = await fetch(`${API_BASE}/news/status`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getNewsStatus');
  }
  return res.json();
}


/**
 * Get the dashboard summary (account, capabilities, positions, fills).
 * @param {Object} [opts]
 * @param {number} [opts.since]   - Epoch seconds to limit fills window.
 * @param {number} [opts.tradeLimit] - Maximum closed trades returned.
 * @param {AbortSignal} [opts.signal] - Optional external AbortSignal.
 * @param {number} [opts.timeoutMs=15000] - Request timeout in ms.
 */
export async function getDashboardSummary(opts = {}) {
  const { since, tradeLimit, signal, timeoutMs = 15000 } = opts;

  const url = new URL('/portfolio/summary', API_BASE);
  if (since) url.searchParams.set('since', String(since));
  if (tradeLimit) url.searchParams.set('trade_limit', String(tradeLimit));

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  if (signal) {
    // If caller passes a signal, abort our controller when theirs aborts
    signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true });
  }

  try {
    const res = await fetch(url.toString(), {
      method: 'GET',
      credentials: 'include',            // send session cookie
      headers: { 'Accept': 'application/json' },
      signal: controller.signal,
    });

    // Handle HTTP errors with a useful message
    if (!res.ok) {
      await throwFetchError(res, 'getDashboardSummary');
    }

    return await res.json();
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error('getDashboardSummary: request timed out');
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Get recent news articles.
 * @param {Object} [opts]
 * @param {number} [opts.limit=100] - Number of articles to fetch.
 */
export async function getRecentNews(opts = {}) {
  const { limit = 100, maxAgeHours, sort = 'newest', offset = 0 } = opts;

  const url = new URL('/news/recent', API_BASE);
  url.searchParams.set('limit', String(limit));
  url.searchParams.set('sort', sort);
  url.searchParams.set('offset', String(offset));
  if (maxAgeHours != null) url.searchParams.set('max_age_hours', String(maxAgeHours));

  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });

  if (!res.ok) {
    await throwFetchError(res, 'getRecentNews');
  }

  return await res.json();
}

export async function searchGdeltEvents(query, opts = {}) {
  const { limit = 25, offset = 0 } = opts;
  const url = new URL('/rag/events/search', API_BASE);
  url.searchParams.set('q', query);
  url.searchParams.set('limit', String(limit));
  url.searchParams.set('offset', String(offset));
  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'searchGdeltEvents');
  }
  return res.json();
}

export async function getRelatedGdeltArticles(eventId, opts = {}) {
  const { limit = 20 } = opts;
  const url = new URL(`/rag/events/${encodeURIComponent(eventId)}/related`, API_BASE);
  url.searchParams.set('limit', String(limit));
  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getRelatedGdeltArticles');
  }
  return res.json();
}

export async function getNewsQuality(opts = {}) {
  const { status = 'open', limit = 50 } = opts;
  const url = new URL('/news/quality', API_BASE);
  url.searchParams.set('status', status);
  url.searchParams.set('limit', String(limit));
  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getNewsQuality');
  }
  return res.json();
}

export async function reviewNewsQualityIssue(issueKey, status, note = '') {
  const res = await fetch(`${API_BASE}/news/quality/issues/${encodeURIComponent(issueKey)}`, {
    method: 'POST',
    credentials: 'include',
    headers: csrfHeaders({ 'Content-Type': 'application/json', 'Accept': 'application/json' }),
    body: JSON.stringify({ status, note }),
  });
  if (!res.ok) {
    await throwFetchError(res, 'reviewNewsQualityIssue');
  }
  return res.json();
}

export async function getNewsLlmStatus() {
  const result = await api('/news/llm/status');
  if (!result.ok) throw new Error(result.error || 'Unable to load News LLM status');
  return result.data;
}

export async function getNewsLlmWatch() {
  const result = await api('/news/llm/watch');
  if (!result.ok) throw new Error(result.error || 'Unable to load News LLM activity');
  return result.data;
}

export async function getNewsLlmQuality(refresh = false) {
  const result = await api(`/news/llm/quality${refresh ? '?refresh=true' : ''}`);
  if (!result.ok) throw new Error(result.error || 'Unable to load News LLM quality metrics');
  return result.data;
}

export async function downloadAcceptedNewsTrainingData(limit = 250000) {
  const url = new URL('/news/llm/training-export', API_BASE);
  url.searchParams.set('limit', String(limit));
  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { Accept: 'application/x-ndjson' },
  });
  if (!res.ok) await throwFetchError(res, 'downloadAcceptedNewsTrainingData');
  return res.blob();
}

export async function reviewNewsLlmSample(sample, judgment, note = '') {
  const result = await api('/news/llm/quality/review', {
    method: 'POST',
    body: JSON.stringify({
      article_url: sample.url,
      category: sample.category,
      judgment,
      note,
    }),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to save News LLM validation');
  return result.data;
}

export async function retryNewsLlmFailures(limit = 500) {
  const result = await api(`/news/llm/retry-failed?limit=${encodeURIComponent(limit)}`, {
    method: 'POST',
  });
  if (!result.ok) throw new Error(result.error || 'Unable to retry failed News LLM records');
  return result.data;
}

export async function getSentimentModelStatus() {
  const result = await api('/news/sentiment/status');
  if (!result.ok) throw new Error(result.error || 'Unable to load sentiment model status');
  return result.data;
}

export async function searchSentimentModels(query = '') {
  const result = await api(`/news/sentiment/catalog?q=${encodeURIComponent(query)}`);
  if (!result.ok) throw new Error(result.error || 'Unable to search Hugging Face sentiment models');
  return result.data;
}

export async function loadSentimentModel(model, labelMap = {}) {
  const result = await api('/news/sentiment/model', {
    method: 'POST',
    body: JSON.stringify({ model, label_map: labelMap }),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to load sentiment model');
  return result.data;
}

export async function unloadSentimentModel() {
  const result = await api('/news/sentiment/model', { method: 'DELETE' });
  if (!result.ok) throw new Error(result.error || 'Unable to unload sentiment model');
  return result.data;
}

export async function previewFinancialSentiment(article) {
  const result = await api('/news/sentiment/preview', {
    method: 'POST',
    body: JSON.stringify(article),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to preview financial sentiment');
  return result.data;
}

export async function evaluateFinancialSentiment(article, expectedLabel) {
  const result = await api('/news/sentiment/evaluate', {
    method: 'POST',
    body: JSON.stringify({ ...article, expected_label: expectedLabel }),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to save sentiment evaluation');
  return result.data;
}

export async function getSentimentEvaluations(limit = 20) {
  const result = await api(`/news/sentiment/evaluations?limit=${encodeURIComponent(limit)}`);
  if (!result.ok) throw new Error(result.error || 'Unable to load sentiment evaluations');
  return result.data;
}

export async function saveSentimentBenchmarkCase(article, expectedLabel) {
  const result = await api('/news/sentiment/benchmark/cases', {
    method: 'POST',
    body: JSON.stringify({ ...article, expected_label: expectedLabel }),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to save sentiment benchmark case');
  return result.data;
}

export async function getSentimentBenchmarkCases(limit = 500, offset = 0) {
  const result = await api(`/news/sentiment/benchmark/cases?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`);
  if (!result.ok) throw new Error(result.error || 'Unable to load sentiment benchmark cases');
  return result.data;
}

export async function importSentimentBenchmarkCases(cases) {
  const result = await api('/news/sentiment/benchmark/import', {
    method: 'POST',
    body: JSON.stringify({ cases }),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to import sentiment benchmark cases');
  return result.data;
}

export async function deleteSentimentBenchmarkCase(caseKey) {
  const result = await api(`/news/sentiment/benchmark/cases/${encodeURIComponent(caseKey)}`, {
    method: 'DELETE',
  });
  if (!result.ok) throw new Error(result.error || 'Unable to delete sentiment benchmark case');
  return result.data;
}

export async function runSentimentBenchmark(limit = 30) {
  const result = await api(`/news/sentiment/benchmark/run?limit=${encodeURIComponent(limit)}`, {
    method: 'POST',
  });
  if (!result.ok) throw new Error(result.error || 'Unable to run sentiment benchmark');
  return result.data;
}

export async function searchNewsLlmCatalog(query = '') {
  const result = await api(`/news/llm/catalog?q=${encodeURIComponent(query)}`);
  if (!result.ok) throw new Error(result.error || 'Unable to search Ollama models');
  return result.data;
}

export async function pullNewsLlmModel(model) {
  const result = await api('/news/llm/models/pull', {
    method: 'POST',
    body: JSON.stringify({ model }),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to pull Ollama model');
  return result.data;
}

export async function deleteNewsLlmModel(model) {
  const result = await api(`/news/llm/models/${encodeURIComponent(model)}`, { method: 'DELETE' });
  if (!result.ok) throw new Error(result.error || 'Unable to delete Ollama model');
  return result.data;
}

export async function updateNewsLlmSettings(settings) {
  const result = await api('/news/llm/settings', {
    method: 'PUT',
    body: JSON.stringify(settings),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to update News LLM settings');
  return result.data;
}

export async function previewNewsLlmArticle(article) {
  const result = await api('/news/llm/preview', {
    method: 'POST',
    body: JSON.stringify(article),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to test the active model');
  return result.data;
}

export async function flagNewsHeadline(article, reason = '') {
  const result = await api('/news/llm/headline-review', {
    method: 'POST',
    body: JSON.stringify({
      article_id: article.dedup_id || article.id || '',
      url: article.canonical_url || article.url,
      original_title: article.original_title || article.title || '',
      display_title: article.display_headline || article.title || '',
      reason: reason || 'Headline looks incorrect or malformed.',
    }),
  });
  if (!result.ok) throw new Error(result.error || 'Unable to flag headline');
  return result.data;
}

export async function getMarketOverview() {
  const res = await fetch(`${API_BASE}/portfolio/market_overview`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getMarketOverview');
  }
  return res.json();
}

/**
 * Get extracted market calendar events from recent news.
 * The backend may use deterministic rules or an optional configured LLM.
 */
export async function getNewsEvents(opts = {}) {
  const { limit = 100, startDate, endDate, includeGenerated } = opts;

  const url = new URL('/news/events', API_BASE);
  url.searchParams.set('limit', String(limit));
  if (startDate) url.searchParams.set('start_date', startDate);
  if (endDate) url.searchParams.set('end_date', endDate);
  if (includeGenerated != null) url.searchParams.set('include_generated', String(includeGenerated));

  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });

  if (!res.ok) {
    await throwFetchError(res, 'getNewsEvents');
  }

  return await res.json();
}

export async function getCalendarState() {
  const res = await fetch(`${API_BASE}/calendar/state`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });

  if (!res.ok) {
    await throwFetchError(res, 'getCalendarState');
  }

  return await res.json();
}

export async function updateCalendarState(state) {
  const res = await fetch(`${API_BASE}/calendar/state`, {
    method: 'PUT',
    credentials: 'include',
    headers: csrfHeaders({ 'Content-Type': 'application/json', 'Accept': 'application/json' }),
    body: JSON.stringify(state),
  });

  if (!res.ok) {
    await throwFetchError(res, 'updateCalendarState');
  }

  return await res.json();
}

/**
 * Get scanner focus results.
 */
export async function getScannerFocus() {
  const res = await fetch(`${API_BASE}/scanner/focus`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });

  if (!res.ok) {
    await throwFetchError(res, 'getScannerFocus');
  }

  return await res.json();
}

export async function getScannerWatchlists() {
  const res = await fetch(`${API_BASE}/scanner/watchlists`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getScannerWatchlists');
  }
  return res.json();
}

export async function getScannerSettings() {
  const res = await fetch(`${API_BASE}/scanner/settings`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getScannerSettings');
  }
  return res.json();
}

export async function updateScannerSettings(settings) {
  const res = await fetch(`${API_BASE}/scanner/settings`, {
    method: 'PUT',
    credentials: 'include',
    headers: csrfHeaders({ 'Content-Type': 'application/json', 'Accept': 'application/json' }),
    body: JSON.stringify(settings),
  });
  if (!res.ok) {
    await throwFetchError(res, 'updateScannerSettings');
  }
  return res.json();
}

export async function getSchwabScannerDecision(symbol) {
  const res = await fetch(`${API_BASE}/scanner/schwab/decision/${encodeURIComponent(symbol)}`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getSchwabScannerDecision');
  }
  return res.json();
}

export async function getScannerContext(symbol) {
  const res = await fetch(`${API_BASE}/scanner/context/${encodeURIComponent(symbol)}`, {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getScannerContext');
  }
  return res.json();
}

export async function getSchwabScannerQuotes(symbols) {
  const normalized = [...new Set(
    (symbols || []).map((symbol) => String(symbol).trim().toUpperCase()).filter(Boolean),
  )];
  const url = new URL('/scanner/schwab/quotes', API_BASE);
  url.searchParams.set('symbols', normalized.join(','));
  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getSchwabScannerQuotes');
  }
  return res.json();
}

export async function getPriceHistory(symbol, opts = {}) {
  const url = new URL('/portfolio/price_history', API_BASE);
  url.searchParams.set('symbol', symbol);
  url.searchParams.set('period_type', opts.periodType || 'day');
  url.searchParams.set('period', String(opts.period || 10));
  url.searchParams.set('frequency_type', opts.frequencyType || 'minute');
  url.searchParams.set('frequency', String(opts.frequency || 5));
  if (opts.lookbackDays) url.searchParams.set('lookback_days', String(opts.lookbackDays));

  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getPriceHistory');
  }
  return res.json();
}

export async function getScannerDecisionHistory(opts = {}) {
  const url = new URL('/scanner/decisions/history', API_BASE);
  if (opts.symbol) url.searchParams.set('symbol', opts.symbol);
  if (opts.limit) url.searchParams.set('limit', String(opts.limit));
  if (opts.offset) url.searchParams.set('offset', String(opts.offset));
  const res = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    await throwFetchError(res, 'getScannerDecisionHistory');
  }
  return res.json();
}

async function researchApi(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const result = await api(path, { signal: controller.signal });
    if (controller.signal.aborted) throw new Error('Request timed out; retry when the server is less busy');
    return result;
  } finally {
    clearTimeout(timer);
  }
}

export async function getHistoricalOutcomeStatus() {
  const result = await researchApi('/agent/evaluations/historical-outcomes/status');
  if (!result.ok) throw new Error(result.error || 'Unable to load historical outcome status');
  return result.data;
}

export async function getTrainingDataStatus() {
  const result = await researchApi('/agent/evaluations/training-data/status');
  if (!result.ok) throw new Error(result.error || 'Unable to load training data status');
  return result.data;
}

export async function getLiveLearningStatus() {
  const result = await researchApi('/agent/evaluations/live-learning/status');
  if (!result.ok) throw new Error(result.error || 'Unable to load live learning status');
  return result.data;
}

export async function getHistoricalPriceStatus() {
  const result = await researchApi('/agent/evaluations/historical-prices/status');
  if (!result.ok) throw new Error(result.error || 'Unable to load historical price status');
  return result.data;
}

export async function getHistoricalOutcomeReport(
  horizon = 60,
  folds = 4,
  featureVersion = 'news-hype-v1',
  generatorVersion = 'news-hype-candidate-generator-v1',
) {
  const params = new URLSearchParams({
    horizon: String(horizon),
    folds: String(folds),
    feature_version: featureVersion,
    generator_version: generatorVersion,
  });
  const result = await researchApi(`/agent/evaluations/historical-outcomes/report?${params}`);
  if (!result.ok) throw new Error(result.error || 'Unable to load historical outcome report');
  return result.data;
}

export async function getResearchExperiments() {
  const result = await researchApi('/agent/evaluations/experiments');
  if (!result.ok) throw new Error(result.error || 'Unable to load research experiments');
  return result.data;
}

export async function getLiveResearchStatus() {
  const result = await researchApi('/agent/evaluations/experiments/live/status');
  if (!result.ok) throw new Error(result.error || 'Unable to load live research status');
  return result.data;
}

export async function getLiveResearchReport() {
  const result = await researchApi('/agent/evaluations/experiments/live/report');
  if (!result.ok) throw new Error(result.error || 'Unable to load live research report');
  return result.data;
}

export async function getShadowEntrySummary() {
  const result = await researchApi('/agent/evaluations/shadow/summary');
  if (!result.ok) throw new Error(result.error || 'Unable to load shadow entry summary');
  return result.data;
}

export async function getShadowEntryCandidates(limit = 100) {
  const result = await researchApi(`/agent/evaluations/shadow/opportunities?limit=${encodeURIComponent(limit)}`);
  if (!result.ok) throw new Error(result.error || 'Unable to load shadow entry opportunities');
  return result.data;
}
