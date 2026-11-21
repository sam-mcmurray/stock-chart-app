// frontend/src/__tests__/Dashboard.test.jsx
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Dashboard from '../pages/Dashboard';
import * as api from '../lib/api';

// Mock the API and hooks used by the component
vi.mock('../lib/api', () => ({
  buildWebSocketUrl: vi.fn(() => 'wss://127.0.0.1:8443/ws/acct'),
  getDashboardSummary: vi.fn(),
  getMarketOverview: vi.fn(),
  getStopLossStatus: vi.fn(),
  getTradingStatus: vi.fn(),
  updateTradingStatus: vi.fn(),
}));

vi.mock('../hooks/useWebSocket', () => ({
  default: vi.fn(() => ({ lastMessage: null, isConnected: true })),
}));

// Mock the BucketHistogram component as its rendering is not the focus here
vi.mock('../components/BucketHistogram.jsx', () => ({
  default: () => <div data-testid="bucket-histogram-mock"></div>,
}));

describe('Dashboard', () => {
  const mockDashboardData = {
    account: {
      equity: 123456.78,
      cash: 54321.0,
      day_pl: 1234.56,
      unrealized_pl: 5000.0,
      realized_pl: 2500.0,
    },
    price_buckets: [
      { bucket: '$10-20', trades: 5, win_rate: 80, pl: 100, avg_pl: 20 },
    ],
    recent_trades: [
      { symbol: 'AAPL', qty: 10, entry: 150, exit: 155, pl: 50, bucket: '$150-160' },
    ],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    api.getTradingStatus.mockResolvedValue({
      auto_trader_enabled: false,
      has_trading_credentials: false,
    });
    api.getStopLossStatus.mockResolvedValue({
      heartbeat: {
        status: 'ok',
        checked: 0,
        triggered: 0,
      },
      heartbeat_age_seconds: null,
      recent_events: [],
      recent_execution_count: 0,
      execute_trades: false,
      monitor_enabled: true,
    });
    api.updateTradingStatus.mockResolvedValue({
      auto_trader_enabled: true,
      has_trading_credentials: true,
    });
    api.getMarketOverview.mockResolvedValue({
      indices: [
        { symbol: '$SPX', name: 'S&P 500', last: 6280.46, change: 18.24, percent_change: 0.29 },
        { symbol: '$DJI', name: 'Dow Jones', last: 44620.11, change: -42.15, percent_change: -0.09 },
      ],
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders loading state initially', () => {
    api.getDashboardSummary.mockReturnValue(new Promise(() => {}));
    api.getTradingStatus.mockReturnValue(new Promise(() => {}));
    api.getStopLossStatus.mockReturnValue(new Promise(() => {}));
    render(<Dashboard />);
    expect(screen.getByText(/Loading dashboard…/i)).toBeInTheDocument();
  });

  it('renders the dashboard with data after successful fetch', async () => {
    api.getDashboardSummary.mockResolvedValue(mockDashboardData);
    render(<Dashboard />);

    // Wait for the loading state to disappear and data to be rendered
    await waitFor(() => {
      // Check KPIs
      expect(screen.getByText('Equity')).toBeInTheDocument();
      expect(screen.getByText('$123,456.78')).toBeInTheDocument();
      
      expect(screen.getByText('Cash')).toBeInTheDocument();
      expect(screen.getByText('$54,321')).toBeInTheDocument();

      expect(screen.getByText('Day P/L')).toBeInTheDocument();
      expect(screen.getByText('$1,234.56')).toBeInTheDocument();

      // Check for sub-components/sections
      expect(screen.getByText('Performance by Entry Price')).toBeInTheDocument();
      expect(screen.getByTestId('bucket-histogram-mock')).toBeInTheDocument();

      expect(screen.getByText('Recent Closed Trades')).toBeInTheDocument();
      expect(screen.getByText('AAPL')).toBeInTheDocument(); // from recent_trades
      expect(screen.getByText('Market Today')).toBeInTheDocument();
      expect(screen.getByText('S&P 500')).toBeInTheDocument();
      expect(screen.getByText('6,280.46')).toBeInTheDocument();
    });
  });

  it('shows explicit empty states when the account has no completed trades', async () => {
    api.getDashboardSummary.mockResolvedValue({
      ...mockDashboardData,
      price_buckets: [],
      recent_trades: [],
    });
    render(<Dashboard />);

    expect(await screen.findByText(/No completed buy and sell pairs/i)).toBeInTheDocument();
    expect(screen.getByText(/No closed trades found in the last 60 days/i)).toBeInTheDocument();
  });

  it('renders an error message on API failure', async () => {
    const errorMessage = 'Network Error';
    api.getDashboardSummary.mockRejectedValue(new Error(errorMessage));
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to load/i)).toBeInTheDocument();
      expect(screen.getByText(new RegExp(errorMessage))).toBeInTheDocument();
    });
  });

  it('refreshes dashboard values when the refresh button is clicked', async () => {
    api.getDashboardSummary
      .mockResolvedValueOnce(mockDashboardData)
      .mockResolvedValueOnce({
        ...mockDashboardData,
        account: { ...mockDashboardData.account, equity: 130000 },
      });

    render(<Dashboard />);

    expect(await screen.findByText('$123,456.78')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }));

    expect(await screen.findByText('$130,000')).toBeInTheDocument();
    expect(api.getDashboardSummary).toHaveBeenCalledTimes(2);
  });

  it('auto-refreshes dashboard values when enabled', async () => {
    api.getDashboardSummary.mockResolvedValue(mockDashboardData);

    render(<Dashboard />);

    await screen.findByText('$123,456.78');
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole('checkbox', { name: /auto refresh/i }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(api.getDashboardSummary).toHaveBeenCalledTimes(2);
  });

  it('loads and updates the auto trader radio control', async () => {
    api.getDashboardSummary.mockResolvedValue(mockDashboardData);
    api.getTradingStatus
      .mockResolvedValueOnce({
        auto_trader_enabled: false,
        has_trading_credentials: false,
      })
      .mockResolvedValue({
        auto_trader_enabled: true,
        has_trading_credentials: true,
      });
    api.updateTradingStatus.mockResolvedValue({
      auto_trader_enabled: true,
      has_trading_credentials: true,
    });
    api.getStopLossStatus.mockResolvedValue({
      heartbeat: {
        status: 'ok',
        checked: 4,
        triggered: 1,
      },
      heartbeat_age_seconds: 12,
      recent_events: [{ symbol: 'AAPL' }],
      recent_execution_count: 2,
      execute_trades: false,
      monitor_enabled: true,
    });

    render(<Dashboard />);

    const onRadio = await screen.findByRole('radio', { name: /on/i });
    const offRadio = screen.getByRole('radio', { name: /off/i });
    expect(offRadio).toBeChecked();

    fireEvent.click(onRadio);

    await waitFor(() => {
      expect(api.updateTradingStatus).toHaveBeenCalledWith(true);
      expect(onRadio).toBeChecked();
    });
    expect(await screen.findByText(/Worker ok/i)).toBeInTheDocument();
    expect(screen.getByText(/12s/i)).toBeInTheDocument();
    expect(screen.getByText(/4 checked/i)).toBeInTheDocument();
    expect(screen.getByText(/2 executions/i)).toBeInTheDocument();
  });
});
