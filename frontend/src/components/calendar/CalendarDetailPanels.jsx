import { useMemo, useState } from 'react';
import { Link, useInRouterContext } from 'react-router-dom';
import {
  autoEventFilters,
  confidenceTone,
  fmtMoney,
  hasReview,
  planEventTypes,
  reviewTagOptions,
} from '../../lib/calendarUtils';

function eventExtractionLabel(source) {
  if (source === 'llm') return 'LLM';
  if (source === 'generated') return 'Generated';
  if (source === 'ipo_scoop_calendar') return 'Expected trade date';
  if (source === 'ipo_scoop_pricing') return 'Offer date';
  if (source === 'ipo_scoop_buzz') return 'Publication date';
  if (source === 'yahoo_earnings') return 'Calendar provider';
  if (source) return 'Rules';
  return '';
}

function earningsEventMeta(event) {
  if (event.extractionSource !== 'yahoo_earnings') return [];
  const details = event.details || {};
  const timing = {
    BMO: 'Before market open',
    AMC: 'After market close',
    DMH: 'During market hours',
    TAS: 'Time not supplied',
  }[details.call_time] || 'Time unknown';
  const values = [timing];
  if (Number.isFinite(details.eps_estimate)) values.push(`EPS est. ${details.eps_estimate}`);
  if (Number.isFinite(details.eps_actual)) values.push(`EPS actual ${details.eps_actual}`);
  if (Number.isFinite(details.surprise_percent)) {
    const sign = details.surprise_percent > 0 ? '+' : '';
    values.push(`Surprise ${sign}${details.surprise_percent}%`);
  }
  if (details.market_cap_text) values.push(`Market cap ${details.market_cap_text}`);
  return values;
}

function formatEps(value) {
  if (!Number.isFinite(value)) return 'N/A';
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function formatSurprise(value) {
  if (!Number.isFinite(value)) return 'N/A';
  return `${value > 0 ? '+' : ''}${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
}

function earningsTiming(callTime) {
  return {
    BMO: 'Before open',
    AMC: 'After close',
    DMH: 'Market hours',
    TAS: 'Not supplied',
  }[callTime] || 'Unknown';
}

function ScannerSymbolLink({ symbol }) {
  const inRouter = useInRouterContext();
  const to = `/scanner?symbol=${encodeURIComponent(symbol)}`;
  const label = `$${symbol}`;

  if (!inRouter) {
    return (
      <a className="auto-event-symbol" href={to}>
        {label}
      </a>
    );
  }

  return (
    <Link className="auto-event-symbol" to={to}>
      {label}
    </Link>
  );
}

function SymbolLink({ symbol }) {
  const safeSymbol = symbol || 'UNKNOWN';
  const inRouter = useInRouterContext();

  if (!symbol) return safeSymbol;

  const to = `/scanner?symbol=${encodeURIComponent(symbol)}`;

  if (!inRouter) {
    return (
      <a className="symbol-link" href={to}>
        {safeSymbol}
      </a>
    );
  }

  return (
    <Link className="symbol-link" to={to}>
      {safeSymbol}
    </Link>
  );
}

export function DailyBulletinPanel({ activeDay, review, planningEvents, autoEvents, marketNotes }) {
  const [copyStatus, setCopyStatus] = useState('');
  const [query, setQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState('All');
  const [sourceFilter, setSourceFilter] = useState('All');
  const [bucketFilter, setBucketFilter] = useState('all');
  const [relevanceFilter, setRelevanceFilter] = useState('All');
  const [sortBy, setSortBy] = useState('newest');
  const rawItems = [
    ...activeDay.trades.map((trade, index) => ({
      id: `trade-${trade.symbol || 'unknown'}-${index}`,
      type: 'Trade',
      title: trade.symbol || 'Unknown symbol',
      meta: `${trade.qty ?? 'N/A'} shares / ${fmtMoney(trade.entry)} -> ${fmtMoney(trade.exit)}`,
      tone: Number(trade.pl || 0) >= 0 ? 'gain' : 'loss',
      value: fmtMoney(trade.pl),
      symbols: trade.symbol ? [trade.symbol] : [],
      source: 'Schwab',
      relevance: 'finance',
      relevanceScore: 1,
    })),
    ...planningEvents.map((event) => ({
      id: `plan-${event.id}`,
      type: event.type || 'Planned',
      title: event.details?.display_headline || event.title,
      meta: event.note,
      tone: 'planned',
      source: 'Planning',
      relevance: 'unclassified',
    })),
    ...autoEvents.filter((event) => (
      event.extractionSource !== 'yahoo_earnings'
      && event.details?.calendar_kind !== 'market_news'
    )).map((event, index) => ({
      id: `auto-${event.id || index}`,
      type: event.type,
      title: event.title,
      meta: [
        event.source,
        eventExtractionLabel(event.extractionSource),
        ...earningsEventMeta(event),
      ].filter(Boolean).join(' / '),
      tone: confidenceTone(event.confidence) || 'auto',
      url: event.url,
      symbols: event.symbols || [],
      confidence: event.confidence,
      source: event.source || 'Unknown source',
      summary: event.details?.llm_summary || event.details?.summary || '',
      provider: event.details?.provider || '',
      providers: event.details?.providers || [],
      publishedTs: Number(event.details?.published_ts || 0),
      relevance: event.details?.relevance_label || 'unclassified',
      relevanceScore: Number(event.details?.relevance_score),
      relevanceReason: event.details?.relevance_reason || '',
      relatedCompanies: event.details?.related_companies || [],
    })),
    ...marketNotes.map((article, index) => ({
      id: `note-${article.url || article.title || index}`,
      type: 'Market note',
      title: article.title || 'Untitled market note',
      meta: [
        article.source || 'Unknown source',
        (article.source_urls || []).length > 1 ? `${article.source_urls.length} related sources` : '',
      ].filter(Boolean).join(' / '),
      tone: 'note',
      url: article.url,
      symbols: article.tickers || [],
      source: article.source || 'Unknown source',
      summary: article.summary || '',
      provider: article.provider || '',
      providers: article.providers || [],
      publishedTs: Number(article.published_ts || 0),
      relevance: article.relevance_label || 'unclassified',
      relevanceScore: Number(article.relevance_score),
      relevanceReason: article.relevance_reason || '',
      relatedCompanies: article.related_companies || [],
      topics: article.llm_topics || [],
      commodities: article.related_commodities || [],
      companyRelationships: article.company_relationships || [],
      confidence: Number(article.relevance_confidence),
      aiAnalyzed: true,
      relatedArticles: article.related_articles || [],
    })),
  ];
  const items = (() => {
    const unique = new Map();
    for (const item of rawItems) {
      const key = item.url || item.id;
      if (!unique.has(key)) unique.set(key, item);
    }
    return Array.from(unique.values());
  })();
  const types = useMemo(() => ['All', ...new Set(items.map((item) => item.type).filter(Boolean))], [items]);
  const sources = useMemo(() => ['All', ...new Set(items.flatMap((item) => [item.source, item.provider, ...(item.providers || [])]).filter(Boolean))].sort(), [items]);
  const buckets = useMemo(() => {
    const counts = new Map();
    const add = (key, label, group) => {
      const current = counts.get(key) || { key, label, group, count: 0 };
      current.count += 1;
      counts.set(key, current);
    };
    for (const item of items) {
      (item.symbols || []).forEach((symbol) => add(`ticker:${symbol}`, `$${symbol}`, 'Companies'));
      (item.relatedCompanies || []).forEach((company) => {
        if (company?.name) add(`company:${company.name}`, company.name, 'Companies');
      });
      (item.commodities || []).forEach((commodity) => add(`commodity:${commodity}`, commodity, 'Commodities'));
      (item.topics || []).forEach((topic) => add(`topic:${topic}`, topic, 'Topics'));
    }
    return Array.from(counts.values()).sort((left, right) => (
      left.group.localeCompare(right.group)
      || right.count - left.count
      || left.label.localeCompare(right.label)
    ));
  }, [items]);
  const filteredItems = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = items.filter((item) => {
      if (typeFilter !== 'All' && item.type !== typeFilter) return false;
      if (sourceFilter !== 'All' && ![item.source, item.provider, ...(item.providers || [])].includes(sourceFilter)) return false;
      const bucketKeys = [
        ...(item.symbols || []).map((symbol) => `ticker:${symbol}`),
        ...(item.relatedCompanies || []).filter((company) => company?.name).map((company) => `company:${company.name}`),
        ...(item.commodities || []).map((commodity) => `commodity:${commodity}`),
        ...(item.topics || []).map((topic) => `topic:${topic}`),
      ];
      if (bucketFilter !== 'all' && !bucketKeys.includes(bucketFilter)) return false;
      if (relevanceFilter !== 'All' && item.relevance !== relevanceFilter) return false;
      if (!needle) return true;
      const companies = (item.relatedCompanies || []).flatMap((company) => [company.name, company.ticker_candidate, company.evidence]);
      return [
        item.title, item.summary, item.meta, item.source, item.provider,
        item.relevance, item.relevanceReason, ...(item.providers || []),
        ...(item.symbols || []), ...companies,
        ...(item.topics || []), ...(item.commodities || []),
      ].filter(Boolean).join(' ').toLowerCase().includes(needle);
    });
    return filtered.sort((left, right) => {
      if (sortBy === 'oldest') return (left.publishedTs || 0) - (right.publishedTs || 0);
      if (sortBy === 'relevance') return (right.relevanceScore || 0) - (left.relevanceScore || 0);
      if (sortBy === 'title') return String(left.title).localeCompare(String(right.title));
      if (sortBy === 'source') return String(left.source).localeCompare(String(right.source));
      return (right.publishedTs || 0) - (left.publishedTs || 0);
    });
  }, [bucketFilter, items, query, relevanceFilter, sortBy, sourceFilter, typeFilter]);
  const visibleItems = filteredItems.slice(0, 100);
  const bulletinText = [
    `${activeDay.label} Daily Bulletin`,
    `P/L: ${fmtMoney(activeDay.pl)}`,
    `Trades: ${activeDay.trades.length}`,
    '',
    ...activeDay.trades.map((trade) => (
      `Trade: ${trade.symbol || 'UNKNOWN'} | Qty ${trade.qty ?? 'N/A'} | Entry ${fmtMoney(trade.entry)} | Exit ${fmtMoney(trade.exit)} | P/L ${fmtMoney(trade.pl)}`
    )),
    ...(planningEvents.length ? ['', 'Planning events:'] : []),
    ...planningEvents.map((event) => `- ${event.type || 'Planned'}: ${event.title}${event.note ? ` (${event.note})` : ''}`),
    ...(autoEvents.length ? ['', 'Auto events:'] : []),
    ...autoEvents.map((event) => `- ${event.type}: ${event.title}${event.source ? ` / ${event.source}` : ''}`),
    ...(marketNotes.length ? ['', 'Market notes:'] : []),
    ...marketNotes.map((article) => `- ${article.title || 'Untitled market note'}${article.source ? ` / ${article.source}` : ''}`),
    ...(hasReview(review) ? ['', 'Review:'] : []),
    ...(review.tags?.length ? [`Tags: ${review.tags.join(', ')}`] : []),
    ...(review.notes ? [`Notes: ${review.notes}`] : []),
  ].filter((line) => line !== null && line !== undefined).join('\n');
  const reviewPromptText = [
    'You are a trading performance coach. Review this trading day using the data below.',
    'Focus on process quality, risk, execution, catalysts, and repeatable lessons.',
    'Return concise sections: Summary, What Worked, What Hurt, Risk Notes, Questions To Answer, and Next Session Rules.',
    '',
    bulletinText,
  ].join('\n');

  const copyText = async (text, filename) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyStatus('Copied');
    } catch {
      const blob = new Blob([text], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
      setCopyStatus('Downloaded');
    }
    window.setTimeout(() => setCopyStatus(''), 1800);
  };

  return (
    <div className="panel daily-bulletin-panel">
      <div className="daily-bulletin-header">
        <div>
          <h3 className="page-title">Daily Bulletin</h3>
          <p className="section-copy">{activeDay.label}</p>
        </div>
        <div className="daily-bulletin-actions">
          <strong className={activeDay.pl >= 0 ? 'gain' : 'loss'}>{fmtMoney(activeDay.pl)}</strong>
          <button type="button" className="scanner-button" onClick={() => copyText(bulletinText, `daily-bulletin-${activeDay.key}.txt`)}>
            Copy bulletin
          </button>
          <button type="button" className="scanner-button" onClick={() => copyText(reviewPromptText, `daily-review-prompt-${activeDay.key}.txt`)}>
            Copy LLM prompt
          </button>
          {copyStatus && <span className="muted-small">{copyStatus}</span>}
        </div>
      </div>

      <div className="daily-bulletin-controls" aria-label="Daily bulletin filters">
        <label className="daily-bulletin-search">
          <span>Search</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Company, ticker, topic, source" />
        </label>
        <label><span>Type</span><select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>{types.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label><span>Source / provider</span><select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}>{sources.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>
          <span>Bucket</span>
          <select
            aria-label="Daily bulletin bucket"
            value={bucketFilter}
            onChange={(event) => setBucketFilter(event.target.value)}
          >
            <option value="all">All items ({items.length})</option>
            {['Companies', 'Commodities', 'Topics'].map((group) => (
              <optgroup key={group} label={group}>
                {buckets.filter((item) => item.group === group).map((item) => (
                  <option key={item.key} value={item.key}>{item.label} ({item.count})</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label><span>Relevance</span><select value={relevanceFilter} onChange={(event) => setRelevanceFilter(event.target.value)}>{['All', 'finance', 'business', 'markets', 'uncertain', 'unclassified'].map((value) => <option key={value}>{value}</option>)}</select></label>
        <label><span>Sort</span><select value={sortBy} onChange={(event) => setSortBy(event.target.value)}><option value="newest">Newest</option><option value="oldest">Oldest</option><option value="relevance">Relevance</option><option value="title">Title</option><option value="source">Source</option></select></label>
        <span className="daily-bulletin-result-count">{filteredItems.length} results</span>
      </div>

      {hasReview(review) && (
        <div className="daily-bulletin-review">
          {review.tags?.length > 0 && (
            <div className="review-tags">
              {review.tags.map((tag) => (
                <span key={tag} className="review-tag review-tag-active">{tag}</span>
              ))}
            </div>
          )}
          {review.notes && <p>{review.notes}</p>}
        </div>
      )}

      {visibleItems.length ? (
        <div className="daily-bulletin-list">
          {visibleItems.map((item) => (
            <div key={item.id} className={`daily-bulletin-item daily-bulletin-${item.tone || 'neutral'}`}>
              <div className="daily-bulletin-body">
                <div className="daily-bulletin-labels">
                  <span className="planning-event-type">{item.type}</span>
                  {item.symbols?.length > 0 && item.type !== 'Trade' && (
                    <span className="auto-event-symbols">
                      {item.symbols.map((symbol) => (
                        <ScannerSymbolLink key={symbol} symbol={symbol} />
                      ))}
                    </span>
                  )}
                  {(item.commodities || []).map((commodity) => (
                    <span key={commodity} className="news-commodity">Commodity: {commodity}</span>
                  ))}
                  {item.relevance && (
                    <span className={`calendar-relevance calendar-relevance-${item.relevance}`}>{item.relevance}</span>
                  )}
                  {Number.isFinite(item.confidence) && (
                    <span className={`status-pill auto-event-confidence-text-${confidenceTone(item.confidence)}`}>
                      {item.aiAnalyzed ? 'AI analyzed ' : ''}{Math.round(item.confidence * 100)}%{item.aiAnalyzed ? '' : ' confidence'}
                    </span>
                  )}
                  {(item.relatedCompanies || []).map((company) => (
                    company?.name && (
                      <span key={company.name} className="status-pill">Company: {company.name}</span>
                    )
                  ))}
                  {(item.topics || []).slice(0, 4).map((topic) => (
                    <span key={topic} className="status-pill status-pill-muted">{topic}</span>
                  ))}
                </div>
                {item.url ? (
                  <a href={item.url} target="_blank" rel="noopener noreferrer" className="auto-event-title">
                    {item.title}
                  </a>
                ) : item.symbols?.length === 1 && item.type === 'Trade' ? (
                  <strong><SymbolLink symbol={item.symbols[0]} /></strong>
                ) : (
                  <strong>{item.title}</strong>
                )}
                {item.meta && <span className="muted-small">{item.meta}</span>}
                {(item.companyRelationships || []).length > 0 && (
                  <span className="market-note-relationships">
                    {item.companyRelationships.slice(0, 3).map((relationship, relationshipIndex) => (
                      <span key={`${relationship.company_a}-${relationship.company_b}-${relationshipIndex}`}>
                        {relationship.company_a} / {relationship.relationship} / {relationship.company_b}
                      </span>
                    ))}
                  </span>
                )}
                {(item.relatedArticles || []).length > 1 && (
                  <details className="calendar-related-coverage">
                    <summary>Related coverage ({item.relatedArticles.length})</summary>
                    <div>
                      {item.relatedArticles.map((article) => (
                        <a
                          key={article.url}
                          href={article.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <strong>{article.source || article.provider || 'Related publisher'}</strong>
                          <span>{article.title || article.url}</span>
                        </a>
                      ))}
                    </div>
                  </details>
                )}
              </div>
              {item.value && <strong className={item.tone}>{item.value}</strong>}
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-panel">No bulletin items match these filters.</div>
      )}
      {filteredItems.length > 100 && (
        <span className="muted-small">Showing the first 100 results. Narrow the bucket or search to continue.</span>
      )}
    </div>
  );
}

export function EarningsEventsPanel({ activeDay, events, articles = [] }) {
  const [query, setQuery] = useState('');
  const [sortBy, setSortBy] = useState('market-cap');
  const earnings = useMemo(
    () => events.filter((event) => event.extractionSource === 'yahoo_earnings'),
    [events],
  );
  const articlesBySymbol = useMemo(() => {
    const groups = new Map();
    for (const article of articles) {
      const symbols = Array.isArray(article.tickers)
        ? article.tickers.map((symbol) => String(symbol).toUpperCase())
        : [];
      for (const symbol of symbols) {
        const group = groups.get(symbol) || [];
        group.push(article);
        groups.set(symbol, group);
      }
    }
    return groups;
  }, [articles]);
  const visibleEarnings = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = earnings.filter((event) => {
      if (!needle) return true;
      return [
        event.symbols?.[0],
        event.details?.company_name,
        event.details?.fiscal_quarter && `Q${event.details.fiscal_quarter}`,
        event.details?.fiscal_year,
      ].filter(Boolean).join(' ').toLowerCase().includes(needle);
    });
    return filtered.sort((left, right) => {
      if (sortBy === 'ticker') {
        return String(left.symbols?.[0] || '').localeCompare(String(right.symbols?.[0] || ''));
      }
      if (sortBy === 'surprise') {
        return (Number(right.details?.surprise_percent) || -Infinity)
          - (Number(left.details?.surprise_percent) || -Infinity);
      }
      if (sortBy === 'timing') {
        return String(left.details?.call_time || '').localeCompare(String(right.details?.call_time || ''));
      }
      return (Number(right.details?.market_cap_value) || -Infinity)
        - (Number(left.details?.market_cap_value) || -Infinity);
    });
  }, [earnings, query, sortBy]);

  if (!earnings.length) return null;

  return (
    <div className="panel earnings-calendar-panel">
      <div className="earnings-calendar-header">
        <div>
          <h3 className="page-title">Earnings</h3>
          <p className="section-copy">{earnings.length} companies reporting on {activeDay.label}.</p>
        </div>
        <span className="muted-small">Yahoo Finance calendar data</span>
      </div>

      <div className="earnings-calendar-controls">
        <label>
          <span>Search</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Company or ticker"
          />
        </label>
        <label>
          <span>Sort</span>
          <select value={sortBy} onChange={(event) => setSortBy(event.target.value)}>
            <option value="market-cap">Market cap</option>
            <option value="surprise">Surprise</option>
            <option value="ticker">Ticker</option>
            <option value="timing">Timing</option>
          </select>
        </label>
        <span className="muted-small">{visibleEarnings.length} of {earnings.length}</span>
      </div>

      <div className="table-scroll">
        <table className="data-table earnings-calendar-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Company</th>
              <th>Timing</th>
              <th>EPS estimate</th>
              <th>Reported EPS</th>
              <th>Surprise</th>
              <th>Market cap</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {visibleEarnings.map((event) => {
              const details = event.details || {};
              const symbol = event.symbols?.[0] || '';
              const linkedArticles = articlesBySymbol.get(symbol) || [];
              const surpriseTone = Number.isFinite(details.surprise_percent)
                ? details.surprise_percent >= 0 ? 'gain' : 'loss'
                : '';

              return (
                <tr key={event.id}>
                  <td><SymbolLink symbol={symbol} /></td>
                  <td>
                    <span className="earnings-company">
                      <strong>{details.company_name || event.title}</strong>
                      {event.details?.fiscal_quarter && event.details?.fiscal_year && (
                        <span className="muted-small">
                          Q{event.details.fiscal_quarter} {event.details.fiscal_year}
                        </span>
                      )}
                      {linkedArticles.length > 0 && (
                        <span className="earnings-linked-articles">
                          {linkedArticles.slice(0, 3).map((article) => (
                            <a
                              key={article.url || article.id}
                              href={article.url}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              {article.title}
                            </a>
                          ))}
                          {linkedArticles.length > 3 && (
                            <span className="muted-small">+{linkedArticles.length - 3} more articles</span>
                          )}
                        </span>
                      )}
                    </span>
                  </td>
                  <td>{earningsTiming(details.call_time)}</td>
                  <td className="numeric-cell">{formatEps(details.eps_estimate)}</td>
                  <td className="numeric-cell">{formatEps(details.eps_actual)}</td>
                  <td className={`numeric-cell ${surpriseTone}`}>{formatSurprise(details.surprise_percent)}</td>
                  <td>{details.market_cap_text || 'N/A'}</td>
                  <td>
                    <span className={`earnings-status earnings-status-${details.status || 'scheduled'}`}>
                      {details.status === 'reported' ? 'Reported' : 'Scheduled'}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {!visibleEarnings.length && <div className="empty-panel">No earnings match this search.</div>}
    </div>
  );
}

export function DayReviewPanel({ activeDay, review, onNotesChange, onToggleTag }) {
  const activeTags = Array.isArray(review.tags) ? review.tags : [];

  return (
    <div className="panel review-panel">
      <div>
        <h3 className="page-title">Review Notes</h3>
        <p className="section-copy">Private notes for {activeDay.label}.</p>
      </div>
      <div className="review-tags" aria-label="Review tags">
        {reviewTagOptions.map((tag) => {
          const selected = activeTags.includes(tag);

          return (
            <button
              key={tag}
              type="button"
              className={`review-tag ${selected ? 'review-tag-active' : ''}`}
              onClick={() => onToggleTag(tag)}
              aria-pressed={selected}
            >
              {tag}
            </button>
          );
        })}
      </div>
      <textarea
        className="review-textarea"
        value={review.notes || ''}
        onChange={(event) => onNotesChange(event.target.value)}
        rows={5}
        placeholder="What mattered on this day?"
      />
    </div>
  );
}

export function PlanningEventsPanel({ activeDay, events, onAddEvent, onUpdateEvent, onDeleteEvent }) {
  const [type, setType] = useState(planEventTypes[0]);
  const [title, setTitle] = useState('');
  const [note, setNote] = useState('');
  const [editingId, setEditingId] = useState('');
  const [editDraft, setEditDraft] = useState({ type: planEventTypes[0], title: '', note: '' });

  const submitEvent = (event) => {
    event.preventDefault();
    if (!title.trim()) return;
    onAddEvent({ type, title, note });
    setTitle('');
    setNote('');
  };

  const startEdit = (event) => {
    setEditingId(event.id);
    setEditDraft({
      type: event.type || planEventTypes[0],
      title: event.title || '',
      note: event.note || '',
    });
  };

  const cancelEdit = () => {
    setEditingId('');
    setEditDraft({ type: planEventTypes[0], title: '', note: '' });
  };

  const submitEdit = (event) => {
    event.preventDefault();
    if (!editingId || !editDraft.title.trim()) return;
    onUpdateEvent(editingId, editDraft);
    cancelEdit();
  };

  return (
    <div className="panel planning-panel">
      <div>
        <h3 className="page-title">Planning Events</h3>
        <p className="section-copy">Manual market or discipline events for {activeDay.label}.</p>
      </div>

      {events.length ? (
        <div className="planning-event-list">
          {events.map((event) => {
            const isEditing = editingId === event.id;

            if (isEditing) {
              return (
                <form key={event.id} className="planning-event-edit" onSubmit={submitEdit}>
                  <label className="settings-field">
                    <span>Type</span>
                    <select
                      value={editDraft.type}
                      onChange={(changeEvent) => setEditDraft((draft) => ({ ...draft, type: changeEvent.target.value }))}
                    >
                      {planEventTypes.map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                  </label>
                  <label className="settings-field">
                    <span>Title</span>
                    <input
                      value={editDraft.title}
                      onChange={(changeEvent) => setEditDraft((draft) => ({ ...draft, title: changeEvent.target.value }))}
                    />
                  </label>
                  <label className="settings-field planning-form-wide">
                    <span>Note</span>
                    <input
                      value={editDraft.note}
                      onChange={(changeEvent) => setEditDraft((draft) => ({ ...draft, note: changeEvent.target.value }))}
                    />
                  </label>
                  <div className="planning-edit-actions">
                    <button type="submit" className="scanner-button" disabled={!editDraft.title.trim()}>
                      Save
                    </button>
                    <button type="button" className="planning-delete-button" onClick={cancelEdit}>
                      Cancel
                    </button>
                  </div>
                </form>
              );
            }

            return (
              <div key={event.id} className="planning-event-item">
                <div>
                  <span className="planning-event-type">{event.type}</span>
                  <strong>{event.title}</strong>
                  {event.note && <span className="muted-small">{event.note}</span>}
                </div>
                <div className="planning-event-actions">
                  <button
                    type="button"
                    className="planning-delete-button"
                    onClick={() => startEdit(event)}
                    aria-label={`Edit ${event.title}`}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="planning-delete-button"
                    onClick={() => onDeleteEvent(event.id)}
                    aria-label={`Delete ${event.title}`}
                  >
                    Delete
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty-panel">No manual planning events for this day.</div>
      )}

      <form className="planning-form" onSubmit={submitEvent}>
        <label className="settings-field">
          <span>Type</span>
          <select value={type} onChange={(event) => setType(event.target.value)}>
            {planEventTypes.map((option) => (
              <option key={option} value={option}>{option}</option>
            ))}
          </select>
        </label>
        <label className="settings-field">
          <span>Title</span>
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="FOMC, AAPL earnings, no trading"
          />
        </label>
        <label className="settings-field planning-form-wide">
          <span>Note</span>
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Plan, risk rule, or watch item"
          />
        </label>
        <button type="submit" className="scanner-button" disabled={!title.trim()}>
          Add event
        </button>
      </form>
    </div>
  );
}

export function AutoEventsPanel({ activeDay, events, counts, filter, onFilterChange }) {
  const visibleFilterOptions = autoEventFilters.filter((option) => option !== 'Earnings watch');
  const earningsCount = counts.get('Earnings watch') || 0;

  return (
    <div className="panel planning-panel">
      <div>
        <h3 className="page-title">Auto Events</h3>
        <p className="section-copy">Other scheduled and detected market events for {activeDay.label}.</p>
      </div>

      <div className="auto-event-filters" aria-label="Auto event filters">
        {visibleFilterOptions.map((option) => {
          const count = option === 'All'
            ? Array.from(counts.values()).reduce((sum, value) => sum + value, 0) - earningsCount
            : counts.get(option) || 0;
          return (
            <button
              key={option}
              type="button"
              className={`auto-event-filter ${filter === option ? 'auto-event-filter-active' : ''}`}
              onClick={() => onFilterChange(option)}
            >
              {option} <span>{count}</span>
            </button>
          );
        })}
      </div>

      {events.length ? (
        <div className="planning-event-list">
          {events.map((event, index) => {
            const tone = confidenceTone(event.confidence);

            return (
              <div
                key={`${event.id}-${index}`}
                className={`planning-event-item ${tone ? `auto-event-confidence-${tone}` : ''}`}
              >
                <div>
                  <span className="planning-event-type">{event.type}</span>
                  {event.url ? (
                    <a
                      href={event.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="auto-event-title"
                    >
                      {event.title}
                    </a>
                  ) : (
                    <strong>{event.title}</strong>
                  )}
                  <span className="auto-event-meta">
                    <span>{event.source}</span>
                    {event.extractionSource && (
                      <span>{eventExtractionLabel(event.extractionSource)}</span>
                    )}
                    {Number.isFinite(event.confidence) && (
                      <span className={`auto-event-confidence-text ${tone ? `auto-event-confidence-text-${tone}` : ''}`}>
                        {Math.round(event.confidence * 100)}% confidence
                      </span>
                    )}
                    {earningsEventMeta(event).map((value) => <span key={value}>{value}</span>)}
                  </span>
                  {event.symbols?.length > 0 && (
                    <span className="auto-event-symbols">
                      {event.symbols.map((symbol) => (
                        <ScannerSymbolLink key={symbol} symbol={symbol} />
                      ))}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty-panel">No auto-detected events for this day.</div>
      )}
    </div>
  );
}
