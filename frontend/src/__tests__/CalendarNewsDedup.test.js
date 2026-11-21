import { describe, expect, it } from 'vitest';
import { deduplicateCalendarNews } from '../lib/calendarEvents';

describe('deduplicateCalendarNews', () => {
  it('merges same-day syndicated headlines and preserves related coverage', () => {
    const result = deduplicateCalendarNews([
      {
        id: 'first',
        calendar_date: '2026-07-23',
        title: 'Oil prices rise after tanker attacks',
        url: 'https://publisher-a.example/oil-story?utm_source=feed',
        source: 'Publisher A',
        provider: 'gdelt',
        tickers: ['XOM'],
        llm_topics: ['energy'],
        related_commodities: ['Crude Oil'],
        related_companies: [],
      },
      {
        id: 'second',
        calendar_date: '2026-07-23',
        title: 'Oil prices rise after tanker attacks',
        url: 'https://publisher-b.example/business/oil-story',
        source: 'Publisher B',
        provider: 'yahoo rss',
        tickers: ['CVX'],
        llm_topics: ['geopolitics'],
        related_commodities: ['Crude Oil'],
        related_companies: [{ name: 'Chevron' }],
      },
    ]);

    expect(result).toHaveLength(1);
    expect(result[0].tickers).toEqual(['CVX', 'XOM']);
    expect(result[0].providers).toEqual(expect.arrayContaining([
      'Publisher A', 'Publisher B', 'gdelt', 'yahoo rss',
    ]));
    expect(result[0].source_urls).toHaveLength(2);
    expect(result[0].related_articles).toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'Publisher A', url: 'https://publisher-a.example/oil-story?utm_source=feed' }),
      expect.objectContaining({ source: 'Publisher B', url: 'https://publisher-b.example/business/oil-story' }),
    ]));
    expect(result[0].llm_topics).toEqual(['energy', 'geopolitics']);
    expect(result[0].related_companies).toEqual([{ name: 'Chevron' }]);
  });

  it('keeps different same-day headlines separate', () => {
    const result = deduplicateCalendarNews([
      {
        calendar_date: '2026-07-23',
        title: 'Company reports earnings',
        url: 'https://example.com/earnings',
      },
      {
        calendar_date: '2026-07-23',
        title: 'Company announces acquisition',
        url: 'https://example.com/acquisition',
      },
    ]);

    expect(result).toHaveLength(2);
  });
});
