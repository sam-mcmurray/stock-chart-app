// frontend/src/__tests__/Scanner.test.jsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import Scanner from '../pages/Scanner';
import * as api from '../lib/api';

// Mock the API call
vi.mock('../lib/api', () => ({
  getPriceHistory: vi.fn(),
  getScannerDecisionHistory: vi.fn(),
  getScannerSettings: vi.fn(),
  getScannerStatus: vi.fn(),
  getScannerWatchlists: vi.fn(),
  getSchwabScannerDecision: vi.fn(),
  getSchwabScannerQuotes: vi.fn(),
  updateScannerSettings: vi.fn(),
}));

vi.mock('../components/CandleChart.jsx', () => ({
  default: ({ candles = [], newsEvents = [] }) => (
    <div data-testid="candle-chart">{candles.length} candles {newsEvents.length} news markers</div>
  ),
}));

describe('Scanner', () => {
  const renderScanner = () => render(<MemoryRouter><Scanner /></MemoryRouter>);
  const mockScannerData = {
    actionable: [
      {
        symbol: 'AAPL', last: 150.50, rvol: 2.5, macd_open: true, score: 8.5,
        company: { name: 'Apple Inc.' },
        decision: {
          catalyst: {
            primary: 'unknown', direction: 'neutral',
            sources: [{ type: 'schwab_mover', label: 'Schwab market movers' }],
          },
        },
        news_sentiment: { label: 'positive', article_count: 2, assessed: true, method: 'keyword_rules' },
      },
      { symbol: 'GOOG', last: 2800.75, rvol: 1.8, macd_open: false, score: 6.2 },
    ],
    priority: [],
    higher_float: [],
    lower_volume: [],
    too_expensive: [],
  };

  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    api.getScannerSettings.mockResolvedValue({
      price_min: 0,
      price_max: 20,
      rvol_threshold: 3,
      rvol_window_days: 60,
      float_max: 20000000,
      scan_interval: 30,
    });
    api.getPriceHistory.mockResolvedValue({
      symbol: 'AAPL',
      candles: [
        { datetime: 1717200000000, open: 10, high: 12, low: 9, close: 11, volume: 1000 },
      ],
    });
    api.getScannerDecisionHistory.mockResolvedValue([]);
    api.getSchwabScannerQuotes.mockResolvedValue({ quotes: {}, fetched_ts: 1800000000 });
    api.getScannerStatus.mockResolvedValue({
      heartbeat: {
        status: 'ok',
        market_data_source: 'schwab',
        result_count: 2,
        failed_count: 0,
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders loading state initially', () => {
    api.getScannerWatchlists.mockReturnValue(new Promise(() => {}));
    renderScanner();
    expect(screen.getByText('Live Price')).toBeInTheDocument();
  });

  it('renders "no results" message when data is empty', async () => {
    api.getScannerWatchlists.mockResolvedValue({});
    renderScanner();
    await waitFor(() => {
      expect(screen.getByText(/No scan results available/i)).toBeInTheDocument();
    });
  });

  it('shows the scanner worker credential error', async () => {
    api.getScannerWatchlists.mockResolvedValue({});
    api.getScannerStatus.mockResolvedValue({
      heartbeat: {
        status: 'error',
        error: 'No authenticated Schwab session is available for market data',
      },
    });

    renderScanner();

    expect(await screen.findByText(/Scanner unavailable: No authenticated Schwab session/i)).toBeInTheDocument();
  });

  it('renders the scanner results table after successful fetch', async () => {
    api.getScannerWatchlists.mockResolvedValue(mockScannerData);
    renderScanner();

    await waitFor(() => {
      // Check for table headers
      expect(screen.getByText('Security')).toBeInTheDocument();
      expect(screen.getByText('Live Price')).toBeInTheDocument();
      expect(screen.getAllByText('RVOL').length).toBeGreaterThan(0);
      expect(screen.getAllByText('Decision').length).toBeGreaterThan(0);

      // Check for data rows
      expect(screen.getByText('AAPL')).toBeInTheDocument();
      expect(screen.getByText('Apple Inc.')).toBeInTheDocument();
      expect(screen.getByText('$150.5')).toBeInTheDocument();
      expect(screen.getByText('2.50x')).toBeInTheDocument();
      expect(screen.getByText(/Source: Schwab market movers/i)).toBeInTheDocument();
      expect(screen.getByText('Positive · rules')).toBeInTheDocument();

      expect(screen.getByText('GOOG')).toBeInTheDocument();
      expect(screen.getByText('$2,800.75')).toBeInTheDocument();
      expect(screen.getByText('1.80x')).toBeInTheDocument();
      expect(screen.getByText('Not assessed')).toBeInTheDocument();
    });
  });

  it('saves scanner settings', async () => {
    api.getScannerWatchlists.mockResolvedValue(mockScannerData);
    api.updateScannerSettings.mockResolvedValue({
      price_min: 0,
      price_max: 15,
      rvol_threshold: 3,
      rvol_window_days: 60,
      float_max: 20000000,
      scan_interval: 30,
    });

    renderScanner();

    const maxPrice = await screen.findByLabelText('Max price');
    fireEvent.change(maxPrice, { target: { value: '15' } });
    fireEvent.click(screen.getByRole('button', { name: /save scanner settings/i }));

    await waitFor(() => {
      expect(api.updateScannerSettings).toHaveBeenCalledWith({
        price_min: 0,
        price_max: 15,
        rvol_threshold: 3,
        rvol_window_days: 60,
        float_max: 20000000,
        scan_interval: 30,
      });
    });
  });

  it('refreshes scanner watchlists on demand', async () => {
    api.getScannerWatchlists
      .mockResolvedValueOnce(mockScannerData)
      .mockResolvedValueOnce({
        ...mockScannerData,
        priority: [{ symbol: 'MSFT', last: 410, rvol: 4, macd_open: true }],
      });

    renderScanner();

    expect(await screen.findByText('AAPL')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }));

    expect(await screen.findByText('MSFT')).toBeInTheDocument();
    expect(api.getScannerWatchlists).toHaveBeenCalledTimes(2);
  });

  it('polls scanner watchlists automatically', async () => {
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval').mockImplementation(() => 1);
    vi.spyOn(globalThis, 'clearInterval').mockImplementation(() => {});
    api.getScannerSettings.mockResolvedValue({
      price_min: 0,
      price_max: 20,
      rvol_threshold: 3,
      rvol_window_days: 60,
      float_max: 20000000,
      scan_interval: 5,
    });
    api.getScannerWatchlists.mockResolvedValue(mockScannerData);

    renderScanner();

    await screen.findByText('AAPL');
    expect(screen.getByLabelText(/auto refresh/i)).toBeChecked();

    await waitFor(() => {
      expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 5000);
      expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 1000);
    });
  });

  it('overlays batched live Schwab prices on every watchlist symbol', async () => {
    api.getScannerWatchlists.mockResolvedValue(mockScannerData);
    api.getSchwabScannerQuotes.mockResolvedValue({
      quotes: {
        AAPL: { symbol: 'AAPL', last: 151.25, bid: 151.24, ask: 151.26, mark: 151.25, day_change_pct: 1.25 },
        GOOG: { symbol: 'GOOG', last: 2801.5, bid: 2801.4, ask: 2801.6, mark: 2801.5, day_change_pct: -0.75 },
      },
      fetched_ts: Math.floor(Date.now() / 1000),
    });

    renderScanner();

    expect(await screen.findByText('$151.25')).toBeInTheDocument();
    expect(screen.getByText('$2,801.5')).toBeInTheDocument();
    expect(screen.getAllByLabelText('Live price up')[0]).toHaveClass('movement-up');
    expect(screen.getByLabelText('Day move up')).toHaveTextContent('+1.25%');
    expect(screen.getByLabelText('Day move down')).toHaveTextContent('-0.75%');
    expect(api.getSchwabScannerQuotes).toHaveBeenCalledWith(['AAPL', 'GOOG']);
    expect(screen.getByText(/Live Schwab prices/i)).toBeInTheDocument();
  });

  it('validates scanner settings before saving', async () => {
    api.getScannerWatchlists.mockResolvedValue(mockScannerData);

    renderScanner();

    const minPrice = await screen.findByLabelText('Min price');
    const maxPrice = screen.getByLabelText('Max price');
    fireEvent.change(minPrice, { target: { value: '25' } });
    fireEvent.change(maxPrice, { target: { value: '15' } });
    fireEvent.click(screen.getByRole('button', { name: /save scanner settings/i }));

    expect(await screen.findByText('Min price must be less than or equal to max price.')).toBeInTheDocument();
    expect(api.updateScannerSettings).not.toHaveBeenCalled();
  });

  it('renders an error message on API failure', async () => {
    const errorMessage = 'Scanner API is down';
    api.getScannerWatchlists.mockRejectedValue(new Error(errorMessage));
    renderScanner();

    await waitFor(() => {
      expect(screen.getByText(`Error: ${errorMessage}`)).toBeInTheDocument();
    });
  });

  it('loads price candles when a ticker is selected', async () => {
    api.getScannerWatchlists.mockResolvedValue(mockScannerData);
    api.getSchwabScannerDecision.mockResolvedValue({
      action: 'watch',
      confidence: 0.7,
      technicals: { last: 150.5, rvol: 2.5 },
      company: { float_shares: 10000000 },
      catalyst: {
        primary: 'earnings', direction: 'positive', matches: [],
        sources: [{ type: 'graph_partnership', label: 'Company relationship: partnership' }],
      },
      intraday: { session: { state: 'regular' } },
      trade_plan: {
        entry_reference: 150.5,
        stop_loss: 145,
        take_profit: 165,
        risk_per_share: 5.5,
        risk_reward: 2.6,
      },
      reasons: [],
      articles: [{
        id: 'article-1',
        title: 'Apple announces a new product',
        summary: 'The company announced a new product.',
        published_ts: 1800000000,
        article_url: 'https://example.com/apple',
        source: 'Example News',
        article_verification_status: 'canonical',
        coverage_sequence: 'earliest_in_lookback',
        coverage_count: 1,
      }],
      news_watch: { state: 'fresh', article_count: 1, latest_published_ts: 1800000000 },
    });

    renderScanner();

    fireEvent.click(await screen.findByRole('button', { name: 'AAPL' }));

    await waitFor(() => {
      expect(api.getPriceHistory).toHaveBeenCalledWith('AAPL', {
        periodType: 'day',
        period: 10,
        frequencyType: 'minute',
        frequency: 5,
      });
    });
    expect(await screen.findByRole('link', { name: /Apple announces a new product/i })).toHaveAttribute(
      'href', 'https://example.com/apple'
    );
    expect(screen.getByTestId('candle-chart')).toHaveTextContent('1 news markers');
    expect(screen.getByText('Earliest found this week')).toBeInTheDocument();
    expect(screen.getByText(/Fresh catalyst watch/i)).toBeInTheDocument();
    expect(screen.getByText(/Source: Company relationship: partnership/i)).toBeInTheDocument();
  });
});
