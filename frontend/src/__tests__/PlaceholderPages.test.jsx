import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Calendar from '../pages/Calendar';
import Profile from '../pages/Profile';

vi.mock('../lib/api', () => ({
  buildLoginUrl: vi.fn(() => 'https://127.0.0.1:8443/auth/login?next=%2Fprofile'),
  disconnectSchwab: vi.fn(),
  getAuthStatus: vi.fn(),
  getCalendarState: vi.fn(),
  getDashboardSummary: vi.fn(),
  getNewsEvents: vi.fn(),
  getNewsStatus: vi.fn(),
  getLocalExitReadiness: vi.fn(),
  getRecentNews: vi.fn(),
  getScannerStatus: vi.fn(),
  getStopLossStatus: vi.fn(),
  getTradingStatus: vi.fn(),
  updateCalendarState: vi.fn(),
}));

vi.mock('../lib/hardNav', () => ({
  hardReplace: vi.fn(),
}));

import {
  disconnectSchwab,
  getAuthStatus,
  getCalendarState,
  getDashboardSummary,
  getNewsEvents,
  getNewsStatus,
  getLocalExitReadiness,
  getRecentNews,
  getScannerStatus,
  getStopLossStatus,
  getTradingStatus,
  updateCalendarState,
} from '../lib/api';
import { hardReplace } from '../lib/hardNav';

describe('placeholder pages', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getAuthStatus.mockResolvedValue({
      ok: true,
      data: { authenticated: true, seconds_to_expiry: 3600 },
    });
    getDashboardSummary.mockResolvedValue({
      recent_trades: [
        {
          symbol: 'AAPL',
          qty: 10,
          entry: 150,
          exit: 155,
          pl: 50,
          bucket: '$100+',
          ts: 1717200000,
        },
        {
          symbol: 'TSLA',
          qty: 5,
          entry: 180,
          exit: 175,
          pl: -25,
          bucket: '$100+',
          ts: 1717200000,
        },
      ],
    });
    getRecentNews.mockResolvedValue([
      {
        title: 'Fed keeps rates unchanged',
        source: 'Market Wire',
        url: 'https://example.com/fed',
        published_ts: 1717200000,
        relevance_label: 'markets',
        relevance_confidence: 0.95,
        llm_topics: ['fomc'],
      },
      {
        title: 'New AI company files for IPO',
        source: 'IPO Desk',
        url: 'https://example.com/ipo',
        published_ts: 1717200000,
        relevance_label: 'business',
        relevance_confidence: 0.92,
        llm_topics: ['ipo'],
      },
      {
        title: 'AAPL earnings beat revenue expectations',
        source: 'Earnings Wire',
        url: 'https://example.com/earnings',
        published_ts: 1717200000,
        relevance_label: 'finance',
        relevance_confidence: 0.96,
        llm_topics: ['earnings'],
        related_commodities: ['Crude Oil'],
        tickers: ['AAPL'],
      },
    ]);
    getNewsEvents.mockResolvedValue([]);
    getCalendarState.mockResolvedValue({ reviews: {}, planning_events: {} });
    updateCalendarState.mockResolvedValue({ reviews: {}, planning_events: {} });
    getTradingStatus.mockResolvedValue({
      auto_trader_enabled: false,
      has_trading_credentials: false,
    });
    getScannerStatus.mockResolvedValue({
      ok: true,
      heartbeat_age_seconds: 12,
      focus_count: 4,
      recent_ticker_count: 6,
    });
    getNewsStatus.mockResolvedValue({
      ok: true,
      heartbeat_age_seconds: 20,
      recent_article_count: 10,
      recent_ticker_count: 7,
    });
    getStopLossStatus.mockResolvedValue({
      execute_trades: false,
    });
    getLocalExitReadiness.mockResolvedValue({
      ready: false,
      reasons: ['execution_disabled'],
      protection_mode: 'local_trigger',
      account_stream_age_seconds: 2,
      exit_worker_age_seconds: 1,
      backup_exit_worker_age_seconds: 1,
      database_available: true,
    });
    disconnectSchwab.mockResolvedValue({ ok: true });
  });

  it('renders daily trade P/L bulletins on Calendar', async () => {
    render(<Calendar />);

    expect(screen.getByRole('heading', { name: /calendar/i })).toBeInTheDocument();
    expect((await screen.findAllByText(/2 trades/i)).length).toBeGreaterThan(0);
    expect(screen.getAllByText('$25').length).toBeGreaterThan(0);
    expect(screen.getAllByText('AAPL').length).toBeGreaterThan(0);
    expect(screen.getAllByText('TSLA').length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: /market events/i })).toBeInTheDocument();
    expect(screen.getByText('IPOs')).toBeInTheDocument();
    expect(screen.getByText('Earnings News')).toBeInTheDocument();
    expect(screen.getAllByText('Fed keeps rates unchanged').length).toBeGreaterThan(0);
    expect(screen.getAllByText('New AI company files for IPO').length).toBeGreaterThan(0);
    expect(screen.queryByText('AAPL earnings beat revenue expectations', { selector: '.calendar-event-link span:first-child' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: /Earnings News/ }));
    expect(screen.getAllByText('AAPL earnings beat revenue expectations').length).toBeGreaterThan(0);
    expect(screen.getByRole('tab', { name: /Earnings News/ })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByRole('tab', { name: /Market Notes/ }));
    expect(screen.getByLabelText('Market note bucket')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '$AAPL (1)' })).toBeInTheDocument();
    expect(screen.getByLabelText('Daily bulletin bucket')).toBeInTheDocument();
    expect(screen.getAllByRole('option', { name: 'Crude Oil (1)' }).length).toBeGreaterThan(0);
    expect(screen.getByLabelText('Daily bulletin filters')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('Company, ticker, topic, source'), { target: { value: 'no-such-company' } });
    expect(screen.getByText('No bulletin items match these filters.')).toBeInTheDocument();
  });

  it('renders live Profile status sections', async () => {
    render(<Profile />);

    expect(screen.getByRole('heading', { name: /profile/i })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: /schwab session/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /auto trader/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /scanner worker/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /news worker/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /local exit protection/i })).toBeInTheDocument();
    expect(screen.getByText('execution_disabled')).toBeInTheDocument();
    expect(screen.getByText(/Double-submit cookie enabled/i)).toBeInTheDocument();
    expect(screen.getByText('Off', { selector: '.status-pill' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /disconnect schwab/i })).toBeInTheDocument();
  });

  it('requires confirmation before disconnecting Schwab', async () => {
    render(<Profile />);
    await screen.findByRole('heading', { name: /schwab session/i });

    fireEvent.click(screen.getByRole('button', { name: /disconnect schwab/i }));
    expect(disconnectSchwab).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /confirm disconnect/i }));

    await waitFor(() => expect(disconnectSchwab).toHaveBeenCalledOnce());
    expect(hardReplace).toHaveBeenCalledWith('https://127.0.0.1:8443/auth/login?next=%2Fprofile');
  });
});
