import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { EarningsEventsPanel } from '../components/calendar/CalendarDetailPanels';

describe('EarningsEventsPanel', () => {
  it('renders earnings fields and ticker-linked coverage as a table', () => {
    render(
      <EarningsEventsPanel
        activeDay={{ label: 'July 23, 2026' }}
        events={[
          {
            id: 'earnings-yahoo-nvda',
            title: 'NVDA Earnings Announcement',
            extractionSource: 'yahoo_earnings',
            symbols: ['NVDA'],
            details: {
              company_name: 'NVIDIA Corporation',
              call_time: 'AMC',
              fiscal_quarter: 2,
              fiscal_year: 2027,
              eps_estimate: 1.21,
              eps_actual: 1.28,
              surprise_percent: 5.79,
              market_cap_text: '4.20T',
              status: 'reported',
            },
          },
          {
            id: 'unrelated-event',
            title: 'Fed decision',
            extractionSource: 'rules',
            symbols: [],
            details: {},
          },
        ]}
        articles={[
          {
            id: 'nvda-article',
            title: 'NVIDIA reports quarterly results',
            url: 'https://example.com/nvda-results',
            tickers: ['NVDA'],
          },
          {
            id: 'unlinked-article',
            title: 'General market update mentioning NVIDIA',
            url: 'https://example.com/general',
            tickers: [],
          },
        ]}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Earnings' })).toBeInTheDocument();
    const table = screen.getByRole('table');
    expect(within(table).getByText('NVIDIA Corporation')).toBeInTheDocument();
    expect(within(table).getByText('After close')).toBeInTheDocument();
    expect(within(table).getByText('1.21')).toBeInTheDocument();
    expect(within(table).getByText('1.28')).toBeInTheDocument();
    expect(within(table).getByText('+5.79%')).toBeInTheDocument();
    expect(within(table).getByText('4.20T')).toBeInTheDocument();
    expect(within(table).getByRole('link', { name: 'NVDA' })).toHaveAttribute('href', '/scanner?symbol=NVDA');
    expect(within(table).getByRole('link', { name: 'NVIDIA reports quarterly results' })).toBeInTheDocument();
    expect(screen.queryByText('General market update mentioning NVIDIA')).not.toBeInTheDocument();
    expect(screen.queryByText('Fed decision')).not.toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('Company or ticker'), { target: { value: 'missing' } });
    expect(screen.getByText('No earnings match this search.')).toBeInTheDocument();
  });
});
