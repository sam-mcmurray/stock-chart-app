import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FiAlertTriangle, FiCheck, FiFlag, FiGrid, FiLink, FiList, FiMapPin, FiRefreshCw, FiRss, FiSearch, FiX } from 'react-icons/fi';
import {
  flagNewsHeadline,
  getNewsQuality,
  getRecentNews,
  getRelatedGdeltArticles,
  reviewNewsQualityIssue,
  searchGdeltEvents,
} from '../lib/api';

const timeAgo = (timestamp) => {
  const seconds = Math.floor((new Date() - new Date(timestamp * 1000)) / 1000);
  if (!Number.isFinite(seconds)) return 'Time unavailable';
  if (seconds < 0) return 'Just released';
  let interval = seconds / 31536000;
  if (interval > 1) return Math.floor(interval) + " years ago";
  interval = seconds / 2592000;
  if (interval > 1) return Math.floor(interval) + " months ago";
  interval = seconds / 86400;
  if (interval > 1) return Math.floor(interval) + " days ago";
  interval = seconds / 3600;
  if (interval > 1) return Math.floor(interval) + " hours ago";
  interval = seconds / 60;
  if (interval > 1) return Math.floor(interval) + " minutes ago";
  return Math.floor(seconds) + " seconds ago";
};

const NewsItemSkeleton = () => (
  <div className="news-item news-skeleton">
    <div className="skeleton-line skeleton-title"></div>
    <div className="skeleton-line skeleton-summary"></div>
    <div className="skeleton-line skeleton-summary-short"></div>
    <div className="skeleton-meta">
      <div className="skeleton-line skeleton-source"></div>
      <div className="skeleton-line skeleton-time"></div>
    </div>
  </div>
);

const NEWS_PAGE_SIZE = 100;

const LocationLabel = ({ location, countryCode }) => {
  const known = location && !String(location).toLowerCase().includes('unknown');
  return <span
    className={`news-location ${known ? 'news-location-known' : 'news-location-unknown'}`}
    title="Automated GDELT event location; verify against the publisher article"
  >
    <FiMapPin /> {known ? location : 'Event location unknown'}
    {countryCode && <span className="news-country-code">{countryCode}</span>}
  </span>;
};


export default function NewsFeed(){
  const [news, setNews] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [error, setError] = useState(null);
  const [view, setView] = useState('latest');
  const [eventQuery, setEventQuery] = useState('market earnings merger');
  const [eventResults, setEventResults] = useState([]);
  const [eventCount, setEventCount] = useState(0);
  const [eventLoading, setEventLoading] = useState(false);
  const [eventError, setEventError] = useState('');
  const [eventSearched, setEventSearched] = useState(false);
  const [eventHasMore, setEventHasMore] = useState(false);
  const [eventDisplay, setEventDisplay] = useState('compare');
  const [relatedCoverage, setRelatedCoverage] = useState({});
  const [maxAgeHours, setMaxAgeHours] = useState(24);
  const [sortOrder, setSortOrder] = useState('newest');
  const [quality, setQuality] = useState(null);
  const [qualityLoading, setQualityLoading] = useState(false);
  const [qualityError, setQualityError] = useState('');
  const [qualityUpdating, setQualityUpdating] = useState('');
  const [headlineFlags, setHeadlineFlags] = useState({});
  const [hasMoreNews, setHasMoreNews] = useState(false);
  const nextNewsOffsetRef = useRef(0);

  const fetchNews = useCallback(async ({ initial = false, append = false } = {}) => {
    try {
      if (initial) {
        setLoading(true);
        setNews([]);
        setHasMoreNews(false);
        nextNewsOffsetRef.current = 0;
      } else {
        setRefreshing(true);
      }
      setError(null);
      const data = await getRecentNews({
        limit: NEWS_PAGE_SIZE,
        maxAgeHours,
        sort: sortOrder,
        offset: append ? nextNewsOffsetRef.current : 0,
      });
      const page = Array.isArray(data) ? data : [];
      setHasMoreNews(page.length === NEWS_PAGE_SIZE);
      if (append) {
        nextNewsOffsetRef.current += page.length;
        setNews((current) => {
          const merged = new Map(
            [...current, ...page].map((article, index) => [
              article.dedup_id || article.canonical_url || article.url || `article-${index}`,
              article,
            ]),
          );
          return Array.from(merged.values());
        });
      } else if (initial || nextNewsOffsetRef.current === 0) {
        nextNewsOffsetRef.current = page.length;
        setNews(page);
      } else {
        setNews((current) => {
          const merged = new Map(
            current.map((article, index) => [
              article.dedup_id || article.canonical_url || article.url || `current-${index}`,
              article,
            ]),
          );
          page.forEach((article, index) => {
            merged.set(
              article.dedup_id || article.canonical_url || article.url || `refresh-${index}`,
              article,
            );
          });
          const cutoff = Date.now() / 1000 - maxAgeHours * 3600;
          return Array.from(merged.values())
            .filter((article) => Number(article.published_ts) >= cutoff)
            .sort((left, right) => (
              sortOrder === 'oldest'
                ? Number(left.published_ts) - Number(right.published_ts)
                : Number(right.published_ts) - Number(left.published_ts)
            ));
        });
      }
    } catch (err) {
      setError(err);
    } finally {
      if (initial) setLoading(false);
      setRefreshing(false);
    }
  }, [maxAgeHours, sortOrder]);
  const loadedNewsRange = useMemo(() => {
    const timestamps = news.map((article) => Number(article.published_ts)).filter(Number.isFinite);
    if (!timestamps.length) return '';
    const oldest = new Date(Math.min(...timestamps) * 1000).toLocaleString();
    const newest = new Date(Math.max(...timestamps) * 1000).toLocaleString();
    return `${news.length.toLocaleString()} loaded / ${oldest} to ${newest}`;
  }, [news]);

  const flagHeadline = async (article) => {
    const key = article.dedup_id || article.canonical_url || article.url;
    setHeadlineFlags((current) => ({ ...current, [key]: 'saving' }));
    try {
      await flagNewsHeadline(article);
      setHeadlineFlags((current) => ({ ...current, [key]: 'flagged' }));
    } catch (flagError) {
      setHeadlineFlags((current) => ({ ...current, [key]: flagError.message || 'Flag failed' }));
    }
  };

  useEffect(() => {
    fetchNews({ initial: true });
  }, [fetchNews]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = setInterval(() => {
      fetchNews();
    }, 60_000);
    return () => clearInterval(timer);
  }, [autoRefresh, fetchNews]);

  const searchEvents = useCallback(async ({ append = false } = {}) => {
    const query = eventQuery.trim();
    if (query.length < 2) return;
    setEventSearched(true);
    setEventLoading(true);
    setEventError('');
    try {
      const offset = append ? eventResults.length : 0;
      const result = await searchGdeltEvents(query, { limit: 25, offset });
      const hits = Array.isArray(result?.hits) ? result.hits : [];
      setEventResults((current) => append ? [...current, ...hits] : hits);
      setEventCount(Number(result?.collection_count || 0));
      setEventHasMore(Boolean(result?.has_more));
    } catch (searchError) {
      setEventError(searchError?.message || String(searchError));
    } finally {
      setEventLoading(false);
    }
  }, [eventQuery, eventResults.length]);

  const loadRelatedCoverage = useCallback(async (eventId) => {
    if (!eventId || relatedCoverage[eventId]?.loading) return;
    if (relatedCoverage[eventId]?.articles) {
      setRelatedCoverage((current) => ({ ...current, [eventId]: undefined }));
      return;
    }
    setRelatedCoverage((current) => ({ ...current, [eventId]: { loading: true } }));
    try {
      const result = await getRelatedGdeltArticles(eventId, { limit: 20 });
      setRelatedCoverage((current) => ({ ...current, [eventId]: { articles: result?.articles || [] } }));
    } catch (coverageError) {
      setRelatedCoverage((current) => ({ ...current, [eventId]: { error: coverageError?.message || String(coverageError) } }));
    }
  }, [relatedCoverage]);

  const loadQuality = useCallback(async () => {
    setQualityLoading(true);
    setQualityError('');
    try {
      setQuality(await getNewsQuality({ status: 'open', limit: 50 }));
    } catch (qualityLoadError) {
      setQualityError(qualityLoadError?.message || String(qualityLoadError));
    } finally {
      setQualityLoading(false);
    }
  }, []);

  const resolveQualityIssue = useCallback(async (issueKey, status) => {
    setQualityUpdating(issueKey);
    setQualityError('');
    try {
      await reviewNewsQualityIssue(issueKey, status);
      setQuality((current) => current ? {
        ...current,
        counts: { ...current.counts, open: Math.max(0, Number(current.counts?.open || 0) - 1) },
        issues: (current.issues || []).filter((issue) => issue.issue_key !== issueKey),
      } : current);
    } catch (qualityUpdateError) {
      setQualityError(qualityUpdateError?.message || String(qualityUpdateError));
    } finally {
      setQualityUpdating('');
    }
  }, []);

  const relatedCell = (result) => {
    if (!result.event_id) return 'N/A';
    const state = relatedCoverage[result.event_id];
    const additionalArticles = (state?.articles || []).filter((article) => article.url !== result.url);
    return <div className="gdelt-related">
      <button type="button" onClick={() => loadRelatedCoverage(result.event_id)} disabled={state?.loading}>
        {state?.loading ? 'Loading...' : state?.articles ? 'Hide coverage' : 'Related coverage'}
      </button>
      {state?.error && <span className="state-error">{state.error}</span>}
      {state?.articles && <div className="gdelt-related-links">
        {!additionalArticles.length ? <span>No additional coverage indexed.</span> : additionalArticles.map((article) => (
          <a key={article.url} href={article.url} target="_blank" rel="noopener noreferrer">
            {article.source || new URL(article.url).hostname} · confidence {Number(article.confidence || 0).toFixed(0)}
          </a>
        ))}
      </div>}
    </div>;
  };

  useEffect(() => {
    if (view === 'events' && !eventSearched && !eventLoading) {
      searchEvents();
    }
  }, [view, eventSearched, eventLoading, searchEvents]);

  useEffect(() => {
    if (view === 'quality' && !quality && !qualityLoading) loadQuality();
  }, [view, quality, qualityLoading, loadQuality]);

  if (error) return <div className="panel">Error: {error.message}</div>;

  return (
    <section className="panel">
      <h2 className="page-title"><FiRss /> News feed</h2>
      <div className="news-view-tabs" role="tablist" aria-label="News views">
        <button type="button" role="tab" aria-selected={view === 'latest'} onClick={() => setView('latest')}>Latest</button>
        <button type="button" role="tab" aria-selected={view === 'events'} onClick={() => setView('events')}>Historical Events</button>
        <button type="button" role="tab" aria-selected={view === 'quality'} onClick={() => setView('quality')}>Review Queue</button>
      </div>

      {view === 'latest' ? <><div className="news-controls">
        <button
          type="button"
          onClick={() => fetchNews()}
          disabled={loading || refreshing}
          className="scanner-button"
        >
          <FiRefreshCw /> {refreshing ? 'Refreshing...' : 'Refresh'}
        </button>
        <label className="scanner-toggle">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(event) => setAutoRefresh(event.target.checked)}
          />
          Auto refresh every 60s
        </label>
        <label className="news-filter">
          <span>Age</span>
          <select value={maxAgeHours} onChange={(event) => setMaxAgeHours(Number(event.target.value))}>
            <option value={6}>Last 6 hours</option>
            <option value={24}>Last 24 hours</option>
            <option value={72}>Last 3 days</option>
            <option value={168}>Last 7 days</option>
            <option value={336}>Last 14 days</option>
          </select>
        </label>
        <label className="news-filter">
          <span>Order</span>
          <select value={sortOrder} onChange={(event) => setSortOrder(event.target.value)}>
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
        </label>
      </div>

      <div className="news-list">
        {loadedNewsRange && <div className="news-loaded-range">{loadedNewsRange}</div>}
        {loading ? (
          Array.from({ length: 5 }).map((_, i) => <NewsItemSkeleton key={i} />)
        ) : !news.length ? (
          <p>No news available. Ensure the news worker is running.</p>
        ) : (
          news.map((article, index) => (
            <div key={article.dedup_id || article.canonical_url || article.url || index} className="news-item">
              <div className="news-primary-labels">
                {(article.tickers || []).map((ticker) => (
                  <a
                    className="news-ticker"
                    href={`/scanner?symbol=${encodeURIComponent(ticker)}`}
                    key={ticker}
                    title={`Open ${ticker} in Scanner`}
                  >
                    {ticker}
                  </a>
                ))}
                {(article.related_commodities || []).map((commodity) => (
                  <span className="news-commodity" key={`${article.dedup_id || article.url}-${commodity}`}>
                    Commodity: {commodity}
                  </span>
                ))}
                {!article.relevance_label && <span className="status-pill status-pill-muted">Pending AI analysis</span>}
                {article.relevance_label && !(article.tickers || []).length && !(article.related_commodities || []).length && (
                  <span className="news-no-ticker">No public ticker identified</span>
                )}
              </div>
              <a href={article.url} target="_blank" rel="noopener noreferrer" className="news-link">
                <h3 className="news-title">{article.title}</h3>
              </a>
              <div className="news-labels">
                {article.title_inferred && <span className="status-pill status-pill-muted">Inferred title</span>}
                {article.headline_source === 'llm' && <span className="status-pill">AI headline</span>}
                {article.headline_source === 'llm_unverified' && <span className="status-pill status-pill-muted">Unverified AI headline</span>}
                {article.headline_source === 'gdelt_mention' && <span className="status-pill">Mention headline</span>}
                {article.translated_to_english && <span className="status-pill">
                  Translated from {article.source_language}
                </span>}
                {article.relevance_label && <span className="status-pill status-pill-muted">{article.relevance_label}</span>}
                {article.relevance_label && <span className="status-pill status-pill-ok">
                  AI analyzed {Math.round(Number(article.relevance_confidence || 0) * 100)}%
                </span>}
                {(article.related_companies || []).map((company) => (
                  <span className="status-pill" key={`${article.dedup_id || article.url}-${company.name}`}>
                    Company: {company.name}
                  </span>
                ))}
                {(article.llm_topics || []).slice(0, 3).map((topic) => (
                  <span className="status-pill status-pill-muted" key={`${article.dedup_id || article.url}-${topic}`}>
                    {topic}
                  </span>
                ))}
                {(article.provider || '').startsWith('gdelt') && <LocationLabel location={article.location} countryCode={article.country_code} />}
              </div>
              {(article.company_relationships || []).length > 0 && <div className="news-relationships">
                {(article.company_relationships || []).map((relationship, relationshipIndex) => (
                  <div key={`${article.dedup_id || article.url}-relationship-${relationshipIndex}`}>
                    <FiLink />
                    <strong>{relationship.company_a}</strong>
                    <span>{relationship.relationship}</span>
                    <strong>{relationship.company_b}</strong>
                    <span className={`news-materiality news-materiality-${relationship.materiality}`}>
                      {relationship.materiality} materiality
                    </span>
                  </div>
                ))}
              </div>}
              <p className="news-summary">{article.llm_summary || article.summary}</p>
              <div className="news-meta">
                <span>{article.source}</span>
                {article.author && <span>By {article.author}</span>}
                <span>{(article.providers || [article.provider || 'publisher feed']).join(', ')}</span>
                <span title={new Date(article.published_ts * 1000).toLocaleString()}>{timeAgo(article.published_ts)}</span>
                <time dateTime={new Date(article.published_ts * 1000).toISOString()}>
                  {new Date(article.published_ts * 1000).toLocaleString()}
                </time>
                <button
                  type="button"
                  className="news-headline-flag"
                  onClick={() => flagHeadline(article)}
                  disabled={!article.url || headlineFlags[article.dedup_id || article.canonical_url || article.url] === 'saving'}
                  title="Flag this headline for review"
                >
                  <FiFlag /> {headlineFlags[article.dedup_id || article.canonical_url || article.url] === 'flagged' ? 'Flagged' : 'Review headline'}
                </button>
              </div>
              {article.gdelt_summary && article.gdelt_summary !== article.summary && <details className="gdelt-classification">
                <summary>GDELT event classification</summary>
                <p>{article.gdelt_summary}</p>
              </details>}
              {article.event_id && relatedCell(article)}
            </div>
          ))
        )}
      </div>
      {hasMoreNews && !loading && (
        <div className="news-pagination">
          <button
            type="button"
            className="scanner-button"
            onClick={() => fetchNews({ append: true })}
            disabled={refreshing}
          >
            {refreshing
              ? 'Loading...'
              : sortOrder === 'newest' ? 'Load 100 older' : 'Load 100 newer'}
          </button>
        </div>
      )}</> : view === 'events' ? <div className="gdelt-search-view" role="tabpanel">
        <form className="gdelt-search-form" onSubmit={(event) => { event.preventDefault(); searchEvents(); }}>
          <label htmlFor="gdelt-event-query">Historical event search</label>
          <div>
            <input
              id="gdelt-event-query"
              value={eventQuery}
              onChange={(event) => setEventQuery(event.target.value)}
              minLength={2}
              maxLength={300}
            />
            <button type="submit" disabled={eventLoading} aria-label="Search historical events">
              <FiSearch /> {eventLoading ? 'Searching...' : 'Search'}
            </button>
          </div>
        </form>
        <div className="gdelt-result-toolbar">
          <div className="gdelt-search-meta">
            {eventResults.length.toLocaleString()} ranked results shown from {eventCount.toLocaleString()} indexed GDELT events
          </div>
          <div className="gdelt-display-switch" aria-label="Historical result display">
            <button type="button" aria-pressed={eventDisplay === 'compare'} onClick={() => setEventDisplay('compare')} title="Comparison table">
              <FiGrid /> Compare
            </button>
            <button type="button" aria-pressed={eventDisplay === 'detail'} onClick={() => setEventDisplay('detail')} title="Detailed results">
              <FiList /> Detail
            </button>
          </div>
        </div>
        {eventError ? <div className="state-error">{eventError}</div> : !eventLoading && !eventResults.length ? (
          <div className="empty-panel">No matching historical events.</div>
        ) : eventDisplay === 'compare' ? (
          <div className="table-scroll">
            <table className="gdelt-compare-table">
              <thead><tr><th>Rank</th><th>Headline</th><th>Source</th><th>Event location</th><th>Date</th><th>Event</th><th>Tone</th><th>Distance</th><th>Articles</th></tr></thead>
              <tbody>{eventResults.map((result, index) => (
                <tr key={`${result.url}-${index}`}>
                  <td>{result.rank || index + 1}</td>
                  <td>
                    <a href={result.url} target="_blank" rel="noopener noreferrer">{result.title}</a>
                    {result.title_inferred && <span className="status-pill">Inferred title</span>}
                    {(result.tickers || []).map((ticker) => <span className="news-ticker" key={ticker}>{ticker}</span>)}
                  </td>
                  <td>{result.source}</td>
                  <td><LocationLabel location={result.location} countryCode={result.country_code} /></td>
                  <td>{new Date(result.published_ts * 1000).toLocaleDateString()}</td>
                  <td>{result.event_code || 'N/A'}</td>
                  <td>{Number.isFinite(result.tone) ? result.tone.toFixed(2) : 'N/A'}</td>
                  <td>{Number.isFinite(result.score) ? result.score.toFixed(3) : 'N/A'}</td>
                  <td>{relatedCell(result)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : (
          <div className="gdelt-result-list">
            {eventResults.map((result, index) => (
              <article className="gdelt-result" key={`${result.url}-${index}`}>
                <div className="gdelt-result-heading">
                  <a href={result.url} target="_blank" rel="noopener noreferrer">{result.title}</a>
                  {result.title_inferred && <span className="status-pill">Inferred title</span>}
                  {(result.tickers || []).map((ticker) => <span className="news-ticker" key={ticker}>{ticker}</span>)}
                </div>
                <p>{result.summary}</p>
                <div className="news-meta">
                  <span>{result.source}</span>
                  <span>{timeAgo(result.published_ts)}</span>
                  {result.event_code && <span>Event {result.event_code}</span>}
                  {Number.isFinite(result.tone) && <span>Tone {result.tone.toFixed(2)}</span>}
                </div>
                <LocationLabel location={result.location} countryCode={result.country_code} />
                {relatedCell(result)}
              </article>
            ))}
          </div>
        )}
        {eventHasMore && <button type="button" className="gdelt-load-more" onClick={() => searchEvents({ append: true })} disabled={eventLoading}>
          {eventLoading ? 'Loading...' : 'Load 25 more'}
        </button>}
      </div> : <div className="news-quality-view" role="tabpanel">
        <div className="gdelt-result-toolbar">
          <div>
            <strong>{Number(quality?.counts?.open || 0).toLocaleString()} open exceptions</strong>
            {quality?.report?.completed_ts && <span className="news-quality-audit-time">
              Last audit {new Date(quality.report.completed_ts * 1000).toLocaleString()}
            </span>}
          </div>
          <button type="button" className="scanner-button" onClick={loadQuality} disabled={qualityLoading}>
            <FiRefreshCw /> {qualityLoading ? 'Loading...' : 'Refresh queue'}
          </button>
        </div>
        {quality?.report && <div className="news-quality-metrics">
          <span>Recent records {Number(quality.report.graph?.recent_articles || 0).toLocaleString()}</span>
          <span>Mentions {Number(quality.report.mentions?.mentions || 0).toLocaleString()}</span>
          <span>Events with mentions {Number(quality.report.mentions?.events || 0).toLocaleString()}</span>
          <span>Audit {Number(quality.report.duration_seconds || 0).toFixed(1)}s</span>
        </div>}
        {qualityError && <div className="state-error">{qualityError}</div>}
        {!qualityLoading && !(quality?.issues || []).length ? (
          <div className="empty-panel">No unresolved news quality exceptions.</div>
        ) : <div className="news-quality-list">
          {(quality?.issues || []).map((issue) => <article className="news-quality-issue" key={issue.issue_key}>
            <div className="news-quality-heading">
              <span className={`news-quality-severity news-quality-severity-${issue.severity}`}>
                <FiAlertTriangle /> {issue.severity}
              </span>
              <span>{String(issue.issue_type || '').replaceAll('_', ' ')}</span>
            </div>
            {issue.article_url ? <a href={issue.article_url} target="_blank" rel="noopener noreferrer">{issue.title}</a> : <strong>{issue.title}</strong>}
            {Object.keys(issue.details || {}).length > 0 && <pre>{JSON.stringify(issue.details, null, 2)}</pre>}
            <div className="news-quality-actions">
              <button type="button" onClick={() => resolveQualityIssue(issue.issue_key, 'reviewed')} disabled={qualityUpdating === issue.issue_key} title="Confirm this label or classification">
                <FiCheck /> Verify
              </button>
              <button type="button" onClick={() => resolveQualityIssue(issue.issue_key, 'dismissed')} disabled={qualityUpdating === issue.issue_key} title="Mark this label or classification inaccurate">
                <FiX /> Incorrect
              </button>
            </div>
          </article>)}
        </div>}
      </div>}
    </section>
  );
}

