// frontend/src/__tests__/NewsFeed.test.jsx
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import NewsFeed from '../pages/NewsFeed';
import * as api from '../lib/api';

// Mock the API call
vi.mock('../lib/api', () => ({
  flagNewsHeadline: vi.fn(),
  getRecentNews: vi.fn(),
  searchGdeltEvents: vi.fn(),
  getRelatedGdeltArticles: vi.fn(),
  getNewsQuality: vi.fn(),
  reviewNewsQualityIssue: vi.fn(),
}));

describe('NewsFeed', () => {
  beforeEach(() => {
    // Reset mocks before each test
    vi.clearAllMocks();
    vi.useRealTimers();
    api.searchGdeltEvents.mockResolvedValue({ collection_count: 0, hits: [] });
    api.getNewsQuality.mockResolvedValue({ report: null, counts: { open: 0 }, issues: [] });
    api.reviewNewsQualityIssue.mockResolvedValue({ status: 'reviewed' });
    api.flagNewsHeadline.mockResolvedValue({ ok: true, issue_key: 'f'.repeat(64) });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders loading state initially', () => {
    // Make the API call never resolve to keep it in loading state
    api.getRecentNews.mockReturnValue(new Promise(() => {}));

    const { container } = render(<NewsFeed />);
    expect(container.querySelectorAll('.news-skeleton')).toHaveLength(5);
  });

  it('renders news articles when fetched successfully', async () => {
    const mockNews = [
      {
        url: 'http://example.com/news1',
        title: 'Headline 1',
        summary: 'Summary of news 1',
        published_ts: Math.floor(Date.now() / 1000) - 3600, // 1 hour ago
        source: 'Source A',
        provider: 'gdelt export',
        providers: ['gdelt export', 'finnhub'],
        author: 'Reporter A',
        tickers: ['AMZN'],
      },
      {
        url: 'http://example.com/news2',
        title: 'Headline 2',
        summary: 'Summary of news 2',
        published_ts: Math.floor(Date.now() / 1000) - 86400, // 1 day ago
        source: 'Source B',
      },
    ];

    api.getRecentNews.mockResolvedValue(mockNews);

    render(<NewsFeed />);

    await waitFor(() => {
      expect(screen.getByText('Headline 1')).toBeInTheDocument();
      expect(screen.getByText('Summary of news 1')).toBeInTheDocument();
      expect(screen.getAllByText(/minutes ago|hours ago/i).length).toBeGreaterThan(0);
      expect(screen.getByText('Source A')).toBeInTheDocument();
      expect(screen.getByText('AMZN')).toBeInTheDocument();
      expect(screen.getByText('By Reporter A')).toBeInTheDocument();
      expect(screen.getByText('gdelt export, finnhub')).toBeInTheDocument();

      expect(screen.getByText('Headline 2')).toBeInTheDocument();
      expect(screen.getByText('Summary of news 2')).toBeInTheDocument();
      expect(screen.getAllByText(/hours ago|days ago/i).length).toBeGreaterThan(0);
      expect(screen.getByText('Source B')).toBeInTheDocument();
    });
  });

  it('flags a questionable headline for persistent review', async () => {
    const article = {
      dedup_id: 'article-1',
      url: 'http://example.com/news1',
      title: 'Improved headline',
      original_title: 'bad extracted headline',
      summary: 'Summary',
      published_ts: Math.floor(Date.now() / 1000),
      source: 'Source A',
    };
    api.getRecentNews.mockResolvedValue([article]);

    render(<NewsFeed />);
    fireEvent.click(await screen.findByRole('button', { name: /review headline/i }));

    await waitFor(() => expect(api.flagNewsHeadline).toHaveBeenCalledWith(article));
    expect(screen.getByRole('button', { name: /flagged/i })).toBeInTheDocument();
  });

  it('requests a new freshness window when the age filter changes', async () => {
    api.getRecentNews.mockResolvedValue([]);
    render(<NewsFeed />);
    await screen.findByText(/No news available/i);

    fireEvent.change(screen.getByLabelText('Age'), { target: { value: '6' } });

    await waitFor(() => expect(api.getRecentNews).toHaveBeenLastCalledWith({
      limit: 100,
      maxAgeHours: 6,
      offset: 0,
      sort: 'newest',
    }));
  });

  it('loads older pages within the selected age window', async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      dedup_id: `article-${index}`,
      url: `https://example.test/${index}`,
      title: `Headline ${index}`,
      summary: 'Summary',
      published_ts: Math.floor(Date.now() / 1000) - index * 60,
      source: 'Example',
    }));
    api.getRecentNews
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValueOnce([{
        dedup_id: 'article-older',
        url: 'https://example.test/older',
        title: 'Older headline',
        summary: 'Older summary',
        published_ts: Math.floor(Date.now() / 1000) - 86400,
        source: 'Example',
      }]);

    render(<NewsFeed />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load 100 older' }));

    expect(await screen.findByText('Older headline')).toBeInTheDocument();
    expect(api.getRecentNews).toHaveBeenLastCalledWith({
      limit: 100,
      maxAgeHours: 24,
      offset: 100,
      sort: 'newest',
    });
  });

  it('keeps loaded older pages when current news refreshes', async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      dedup_id: `article-${index}`,
      url: `https://example.test/${index}`,
      title: `Headline ${index}`,
      summary: 'Summary',
      published_ts: Math.floor(Date.now() / 1000) - index * 60,
      source: 'Example',
    }));
    const olderArticle = {
      dedup_id: 'article-older',
      url: 'https://example.test/older',
      title: 'Older headline',
      summary: 'Older summary',
      published_ts: Math.floor(Date.now() / 1000) - 12 * 3600,
      source: 'Example',
    };
    api.getRecentNews
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValueOnce([olderArticle])
      .mockResolvedValueOnce([{
        dedup_id: 'article-new',
        url: 'https://example.test/new',
        title: 'New headline',
        summary: 'New summary',
        published_ts: Math.floor(Date.now() / 1000),
        source: 'Example',
      }]);

    render(<NewsFeed />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load 100 older' }));
    expect(await screen.findByText('Older headline')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }));

    expect(await screen.findByText('New headline')).toBeInTheDocument();
    expect(screen.getByText('Older headline')).toBeInTheDocument();
    expect(api.getRecentNews).toHaveBeenLastCalledWith({
      limit: 100,
      maxAgeHours: 24,
      offset: 0,
      sort: 'newest',
    });
  });

  it('renders error message on API failure', async () => {
    const errorMessage = 'Failed to fetch news';
    api.getRecentNews.mockRejectedValue(new Error(errorMessage));

    render(<NewsFeed />);

    await waitFor(() => {
      expect(screen.getByText(`Error: ${errorMessage}`)).toBeInTheDocument();
    });
  });

  it('renders an empty state when no news is available', async () => {
    api.getRecentNews.mockResolvedValue([]);

    render(<NewsFeed />);

    expect(await screen.findByText(/No news available/i)).toBeInTheDocument();
  });

  it('refreshes news when the refresh button is clicked', async () => {
    api.getRecentNews
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          url: 'http://example.com/news3',
          title: 'Fresh headline',
          summary: 'Fresh summary',
          published_ts: Math.floor(Date.now() / 1000) - 120,
          source: 'Source C',
        },
      ]);

    render(<NewsFeed />);

    await screen.findByText(/No news available/i);
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }));

    expect(await screen.findByText('Fresh headline')).toBeInTheDocument();
    expect(api.getRecentNews).toHaveBeenCalledTimes(2);
  });

  it('auto-refreshes news when enabled', async () => {
    vi.useFakeTimers();
    api.getRecentNews.mockResolvedValue([]);

    render(<NewsFeed />);

    await act(async () => {});
    expect(screen.getByText(/No news available/i)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /auto refresh/i })).toBeChecked();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(api.getRecentNews).toHaveBeenCalledTimes(2);
  });

  it('shows visible model enrichment metadata', async () => {
    api.getRecentNews.mockResolvedValue([{
      dedup_id: 'article-ai',
      url: 'https://example.test/ai',
      title: 'Company reports results',
      summary: 'Original summary',
      llm_summary: 'Grounded model summary',
      relevance_label: 'finance',
      relevance_confidence: 0.94,
      related_companies: [{ name: 'Example Corporation' }],
      related_commodities: ['Crude Oil'],
      llm_topics: ['earnings', 'dividends'],
      published_ts: Math.floor(Date.now() / 1000),
      source: 'Example',
    }]);

    render(<NewsFeed />);

    expect(await screen.findByText('AI analyzed 94%')).toBeInTheDocument();
    expect(screen.getByText('Company: Example Corporation')).toBeInTheDocument();
    expect(screen.getByText('Commodity: Crude Oil')).toBeInTheDocument();
    expect(screen.getByText('Grounded model summary')).toBeInTheDocument();
    expect(screen.getByText('earnings')).toBeInTheDocument();
  });

  it('links validated ticker chips to the scanner detail', async () => {
    api.getRecentNews.mockResolvedValue([{
      dedup_id: 'article-lly',
      url: 'https://example.test/lly',
      title: "Novo Nordisk's lawsuit against Eli Lilly",
      summary: 'Lawsuit summary',
      tickers: ['LLY'],
      published_ts: Math.floor(Date.now() / 1000),
      source: 'Example',
    }]);

    render(<NewsFeed />);

    const tickerLink = await screen.findByRole('link', { name: 'LLY' });
    expect(tickerLink).toHaveAttribute(
      'href',
      '/scanner?symbol=LLY',
    );
    const headline = screen.getByRole('heading', { name: /Novo Nordisk's lawsuit/i });
    expect(tickerLink.compareDocumentPosition(headline) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('shows all company tickers, explicit relationships, and translation status', async () => {
    api.getRecentNews.mockResolvedValue([{
      dedup_id: 'article-partnership',
      url: 'https://example.test/partnership',
      title: 'Luminar works with NVIDIA on lidar',
      summary: 'Translated partnership summary.',
      tickers: ['LAZR', 'NVDA'],
      source_language: 'Spanish',
      translated_to_english: true,
      company_relationships: [{
        company_a: 'Luminar Technologies',
        relationship: 'partnership',
        company_b: 'NVIDIA',
        materiality: 'high',
      }],
      relevance_label: 'business',
      relevance_confidence: 0.93,
      published_ts: Math.floor(Date.now() / 1000),
      source: 'Example',
    }]);

    render(<NewsFeed />);

    expect(await screen.findByRole('link', { name: 'LAZR' })).toHaveAttribute('href', '/scanner?symbol=LAZR');
    expect(screen.getByRole('link', { name: 'NVDA' })).toHaveAttribute('href', '/scanner?symbol=NVDA');
    expect(screen.getByText('Translated from Spanish')).toBeInTheDocument();
    expect(screen.getByText('Luminar Technologies')).toBeInTheDocument();
    expect(screen.getByText('partnership')).toBeInTheDocument();
    expect(screen.getByText('NVIDIA')).toBeInTheDocument();
    expect(screen.getByText('high materiality')).toBeInTheDocument();
  });

  it('searches the separate historical GDELT event collection', async () => {
    api.getRecentNews.mockResolvedValue([]);
    api.searchGdeltEvents.mockResolvedValue({
      collection_count: 8100,
      has_more: false,
      hits: [{
        title: 'Company announces earnings',
        event_id: '1307007199',
        summary: 'Structured GDELT event summary',
        url: 'https://example.com/earnings',
        source: 'example.com',
        published_ts: Math.floor(Date.now() / 1000) - 86400,
        event_code: '010',
        tone: 1.25,
        title_inferred: true,
      }],
    });

    render(<NewsFeed />);
    await screen.findByText(/No news available/i);
    fireEvent.click(screen.getByRole('tab', { name: /historical events/i }));

    expect(await screen.findByText('Company announces earnings')).toBeInTheDocument();
    expect(screen.getByText('Inferred title')).toBeInTheDocument();
    expect(screen.getByText(/1 ranked results shown from 8,100 indexed GDELT events/i)).toBeInTheDocument();
    expect(api.searchGdeltEvents).toHaveBeenCalledWith('market earnings merger', { limit: 25, offset: 0 });
  });

  it('loads related event coverage on demand', async () => {
    api.getRecentNews.mockResolvedValue([]);
    api.searchGdeltEvents.mockResolvedValue({
      collection_count: 1,
      has_more: false,
      hits: [{ event_id: '1307007199', rank: 1, title: 'Merger event', url: 'https://example.com/event', source: 'example.com', published_ts: 1780272000 }],
    });
    api.getRelatedGdeltArticles.mockResolvedValue({
      event_id: '1307007199',
      articles: [{ url: 'https://related.test/story', source: 'related.test', confidence: 84 }],
    });

    render(<NewsFeed />);
    await screen.findByText(/No news available/i);
    fireEvent.click(screen.getByRole('tab', { name: /historical events/i }));
    await screen.findByText('Merger event');
    fireEvent.click(screen.getByRole('button', { name: /related coverage/i }));

    expect(await screen.findByText(/related.test · confidence 84/i)).toBeInTheDocument();
    expect(api.getRelatedGdeltArticles).toHaveBeenCalledWith('1307007199', { limit: 20 });
  });

  it('loads additional ranked historical results', async () => {
    api.getRecentNews.mockResolvedValue([]);
    api.searchGdeltEvents
      .mockResolvedValueOnce({
        collection_count: 50,
        has_more: true,
        hits: [{ rank: 1, title: 'First event', url: 'https://example.com/1', source: 'one.test', published_ts: 1780272000 }],
      })
      .mockResolvedValueOnce({
        collection_count: 50,
        has_more: false,
        hits: [{ rank: 2, title: 'Second event', url: 'https://example.com/2', source: 'two.test', published_ts: 1780272000 }],
      });

    render(<NewsFeed />);
    await screen.findByText(/No news available/i);
    fireEvent.click(screen.getByRole('tab', { name: /historical events/i }));
    await screen.findByText('First event');
    fireEvent.click(screen.getByRole('button', { name: /load 25 more/i }));

    expect(await screen.findByText('Second event')).toBeInTheDocument();
    expect(api.searchGdeltEvents).toHaveBeenLastCalledWith('market earnings merger', { limit: 25, offset: 1 });
  });

  it('shows and resolves a bounded news quality exception', async () => {
    api.getRecentNews.mockResolvedValue([]);
    api.getNewsQuality.mockResolvedValue({
      report: { completed_ts: 1784427000, duration_seconds: 3.2, graph: { recent_articles: 120 }, mentions: { mentions: 140541, events: 63086 } },
      counts: { open: 1 },
      issues: [{
        issue_key: 'a'.repeat(64),
        issue_type: 'ticker_overload',
        severity: 'medium',
        title: 'Article with too many labels',
        article_url: 'https://example.test/article',
        details: { tickers: ['A', 'B', 'C', 'D', 'E'] },
      }],
    });

    render(<NewsFeed />);
    await screen.findByText(/No news available/i);
    fireEvent.click(screen.getByRole('tab', { name: /review queue/i }));
    expect(await screen.findByText('Article with too many labels')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /verify/i }));

    await waitFor(() => expect(api.reviewNewsQualityIssue).toHaveBeenCalledWith('a'.repeat(64), 'reviewed'));
    expect(await screen.findByText(/No unresolved news quality exceptions/i)).toBeInTheDocument();
  });
});
