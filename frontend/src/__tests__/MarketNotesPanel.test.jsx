import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MarketNotesPanel } from '../components/calendar/CalendarPanels';

describe('MarketNotesPanel', () => {
  it('buckets AI-analyzed notes using validated metadata', () => {
    render(
      <MarketNotesPanel
        activeDay={{ label: 'July 23, 2026' }}
        activeNews={[
          {
            id: 'oil',
            title: 'Oil prices rise after supply disruption',
            url: 'https://example.com/oil',
            source: 'Energy Wire',
            tickers: ['XOM'],
            related_companies: [{ name: 'Exxon Mobil' }],
            related_commodities: ['Crude Oil'],
            llm_topics: ['energy'],
            relevance_label: 'finance',
            relevance_confidence: 0.96,
            summary: 'Crude supply constraints lifted energy shares.',
            related_articles: [
              {
                title: 'Oil prices rise after supply disruption',
                url: 'https://example.com/oil',
                source: 'Energy Wire',
              },
              {
                title: 'Oil prices rise after supply disruption',
                url: 'https://syndicated.example.com/oil',
                source: 'Syndicated Wire',
              },
            ],
          },
          {
            id: 'tariffs',
            title: 'New tariffs affect semiconductor imports',
            url: 'https://example.com/tariffs',
            source: 'Trade Wire',
            tickers: ['NVDA'],
            related_companies: [{ name: 'NVIDIA' }],
            related_commodities: [],
            llm_topics: ['trade policy'],
            relevance_label: 'business',
            relevance_confidence: 0.91,
            summary: 'The policy changes import costs.',
          },
        ]}
      />,
    );

    expect(screen.getByRole('option', { name: 'Crude Oil (1)' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'trade policy (1)' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'XOM' })).toHaveAttribute('href', '/scanner?symbol=XOM');
    expect(screen.getByText('Related coverage (2)')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Market note bucket'), { target: { value: 'topic:trade policy' } });
    expect(screen.getByText('New tariffs affect semiconductor imports')).toBeInTheDocument();
    expect(screen.queryByText('Oil prices rise after supply disruption')).not.toBeInTheDocument();
  });
});
