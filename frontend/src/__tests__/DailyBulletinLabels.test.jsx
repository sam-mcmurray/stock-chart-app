import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DailyBulletinPanel } from '../components/calendar/CalendarDetailPanels';

describe('DailyBulletinPanel labels', () => {
  it('keeps article classification labels in one contiguous row', () => {
    render(
      <DailyBulletinPanel
        activeDay={{ key: '2026-07-23', label: 'July 23, 2026', trades: [], pl: 0 }}
        review={{ tags: [], notes: '' }}
        planningEvents={[]}
        autoEvents={[]}
        marketNotes={[
          {
            id: 'article',
            title: 'Oil producer reports earnings',
            url: 'https://example.com/article',
            source: 'Finance Wire',
            tickers: ['XOM'],
            relevance_label: 'finance',
            relevance_confidence: 0.96,
            related_companies: [{ name: 'Exxon Mobil' }],
            related_commodities: ['Crude Oil'],
            llm_topics: ['earnings', 'energy'],
          },
        ]}
      />,
    );

    const labels = screen.getByText('Commodity: Crude Oil').closest('.daily-bulletin-labels');
    expect(labels).not.toBeNull();
    expect(within(labels).getByText('Market note')).toBeInTheDocument();
    expect(within(labels).getByRole('link', { name: '$XOM' })).toBeInTheDocument();
    expect(within(labels).getByText('finance')).toBeInTheDocument();
    expect(within(labels).getByText('AI analyzed 96%')).toBeInTheDocument();
    expect(within(labels).getByText('Company: Exxon Mobil')).toBeInTheDocument();
    expect(within(labels).getByText('earnings')).toBeInTheDocument();
    expect(within(labels).getByText('energy')).toBeInTheDocument();
  });
});
