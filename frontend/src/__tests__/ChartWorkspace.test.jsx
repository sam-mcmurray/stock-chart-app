import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ChartWorkspace from '../pages/ChartWorkspace';
import * as api from '../lib/api';

vi.mock('../lib/api', () => ({
  getPriceHistory: vi.fn(),
  getScannerContext: vi.fn(),
  getSchwabScannerQuotes: vi.fn(),
}));

vi.mock('../components/CandleChart', () => ({
  default: ({ candles, indicators, daily, newsEvents = [] }) => (
    <div data-testid="workspace-chart">
      {candles.length} bars close {candles.at(-1)?.close} {indicators ? 'with indicators' : ''} {daily ? 'daily' : 'intraday'} {newsEvents.length} news
    </div>
  ),
}));

function renderWorkspace(path = '/charts/IBM') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/charts/:symbol" element={<ChartWorkspace />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('ChartWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getPriceHistory.mockResolvedValue({
      candles: [
        { datetime: 3_600_000, open: 10, high: 12, low: 9, close: 11, volume: 100 },
        { datetime: 5_400_000, open: 11, high: 13, low: 10, close: 12, volume: 200 },
      ],
    });
    api.getSchwabScannerQuotes.mockResolvedValue({
      quotes: {
        IBM: {
          symbol: 'IBM',
          last: 13.25,
          fetched_ts: 5_400_000,
          market_ts: 5_400_000,
          realtime: true,
          day_change_pct: 2.5,
        },
      },
      fetched_ts: 5_400_000,
    });
    api.getScannerContext.mockResolvedValue({
      symbol: 'IBM',
      company: { name: 'International Business Machines Corporation' },
      articles: [{
        id: 'article-1', title: 'IBM launches a new platform',
        published_ts: 1800000000, article_url: 'https://example.com/ibm',
      }],
    });
  });

  it('loads all four Schwab timeframes with indicators', async () => {
    renderWorkspace();

    expect(await screen.findByText('IBM Charts')).toBeInTheDocument();
    await waitFor(() => expect(api.getPriceHistory).toHaveBeenCalledTimes(4));

    expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
      periodType: 'day', period: 10, frequencyType: 'minute', frequency: 1,
    });
    expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
      periodType: 'day', period: 10, frequencyType: 'minute', frequency: 5,
    });
    expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
      periodType: 'day', period: 10, frequencyType: 'minute', frequency: 30,
    });
    expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
      periodType: 'year', period: 10, frequencyType: 'daily', frequency: 1,
    });
    expect(await screen.findAllByTestId('workspace-chart')).toHaveLength(4);
    expect(screen.getAllByText(/with indicators/)).toHaveLength(4);
    expect(await screen.findByText('$13.25')).toHaveClass('chart-live-price');
    expect(screen.getByText(/2.50% today/i)).toHaveClass('movement-up');
    expect(screen.getAllByText(/close 13.25/i)).toHaveLength(4);
    expect(screen.getByText('International Business Machines Corporation')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /IBM launches a new platform/i })).toHaveAttribute(
      'href', 'https://example.com/ibm'
    );
    expect(screen.getAllByText(/1 news/i)).toHaveLength(4);
  });

  it('navigates to another chart symbol', async () => {
    renderWorkspace();
    await screen.findAllByTestId('workspace-chart');

    fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: 'aapl' } });
    fireEvent.click(screen.getByRole('button', { name: 'Load symbol' }));

    expect(await screen.findByText('AAPL Charts')).toBeInTheDocument();
    await waitFor(() => expect(api.getPriceHistory).toHaveBeenCalledWith('AAPL', expect.any(Object)));
  });

  it('reloads a timeframe when its range changes', async () => {
    renderWorkspace();
    await waitFor(() => expect(api.getPriceHistory).toHaveBeenCalledTimes(4));

    fireEvent.change(screen.getByLabelText('Daily range'), { target: { value: '5y' } });

    await waitFor(() => {
      expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
        periodType: 'year', period: 5, frequencyType: 'daily', frequency: 1,
      });
    });
    expect(api.getPriceHistory).toHaveBeenCalledTimes(5);
  });

  it('offers the maximum supported extended intraday ranges', async () => {
    renderWorkspace();
    await waitFor(() => expect(api.getPriceHistory).toHaveBeenCalledTimes(4));

    fireEvent.change(screen.getByLabelText('1 Minute range'), { target: { value: '30d' } });
    await waitFor(() => expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
      lookbackDays: 30, frequencyType: 'minute', frequency: 1,
    }));

    fireEvent.change(screen.getByLabelText('5 Minute range'), { target: { value: '60d' } });
    await waitFor(() => expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
      lookbackDays: 60, frequencyType: 'minute', frequency: 5,
    }));

    fireEvent.change(screen.getByLabelText('Hourly range'), { target: { value: '270d' } });
    await waitFor(() => expect(api.getPriceHistory).toHaveBeenCalledWith('IBM', {
      lookbackDays: 270, frequencyType: 'minute', frequency: 30,
    }));
    expect(api.getPriceHistory).toHaveBeenCalledTimes(7);
  });
});
