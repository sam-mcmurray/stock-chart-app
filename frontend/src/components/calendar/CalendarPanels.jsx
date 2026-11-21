import { useMemo, useState } from 'react';
import {
  FiBarChart2,
  FiCalendar,
  FiChevronLeft,
  FiChevronRight,
  FiClock,
  FiTrendingUp,
} from 'react-icons/fi';
import {
  fmtMoney,
  fmtPct,
  formatAutoEventBadges,
} from '../../lib/calendarUtils';

const marketEventIcons = {
  IPOs: <FiTrendingUp />,
  'Earnings News': <FiBarChart2 />,
  'Economic Releases': <FiClock />,
  'Market Notes': <FiCalendar />,
};

export function CalendarSymbolFilter({ symbols, value, tradeCount, onChange }) {
  return (
    <div className="panel calendar-filter-panel">
      <label className="settings-field calendar-symbol-filter">
        <span>Symbol filter</span>
        <select value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="All">All symbols</option>
          {symbols.map((symbol) => (
            <option key={symbol} value={symbol}>{symbol}</option>
          ))}
        </select>
      </label>
      <span className="calendar-filter-summary">
        {value === 'All' ? 'Showing all closed trades' : `Showing ${value}`}
        {' / '}
        {tradeCount.toLocaleString()} trades
      </span>
      {value !== 'All' && (
        <button type="button" className="scanner-button" onClick={() => onChange('All')}>
          Clear
        </button>
      )}
    </div>
  );
}

export function CalendarDayNavigator({ activeDay, onPreviousDay, onNextDay, onJumpToDay }) {
  const inputValue = activeDay.key === 'unscheduled' ? '' : activeDay.key;

  return (
    <div className="panel calendar-day-navigator">
      <button type="button" className="scanner-button calendar-nav-button" onClick={onPreviousDay} aria-label="Previous day">
        <FiChevronLeft />
      </button>
      <label className="settings-field calendar-jump-field">
        <span>Selected day</span>
        <input
          type="date"
          value={inputValue}
          onChange={(event) => {
            if (event.target.value) onJumpToDay(event.target.value);
          }}
        />
      </label>
      <button type="button" className="scanner-button calendar-nav-button" onClick={onNextDay} aria-label="Next day">
        <FiChevronRight />
      </button>
    </div>
  );
}

export function CalendarMonthPanel({
  visibleMonthLabel,
  monthSummary,
  curvePoints,
  monthCells,
  activeKey,
  dayReviews,
  planningEvents,
  onPreviousMonth,
  onNextMonth,
  onToday,
  onSelectDay,
  hasReview,
}) {
  return (
    <div className="panel">
      <div className="calendar-month-header">
        <button
          type="button"
          className="scanner-button calendar-nav-button"
          onClick={onPreviousMonth}
          aria-label="Previous month"
        >
          <FiChevronLeft />
        </button>
        <h3 className="page-title">{visibleMonthLabel}</h3>
        <div className="calendar-month-actions">
          <button type="button" className="scanner-button" onClick={onToday}>
            Today
          </button>
          <button
            type="button"
            className="scanner-button calendar-nav-button"
            onClick={onNextMonth}
            aria-label="Next month"
          >
            <FiChevronRight />
          </button>
        </div>
      </div>
      <div className="calendar-summary-grid">
        <SummaryTile label="Month P/L" value={fmtMoney(monthSummary.totalPl)} tone={monthSummary.totalPl >= 0 ? 'gain' : 'loss'} />
        <SummaryTile label="Trades" value={monthSummary.tradeCount.toLocaleString()} />
        <SummaryTile label="Winning days" value={monthSummary.winningDays.toLocaleString()} tone="gain" />
        <SummaryTile label="Losing days" value={monthSummary.losingDays.toLocaleString()} tone="loss" />
        <SummaryTile label="Max drawdown" value={fmtMoney(monthSummary.maxDrawdown)} tone={monthSummary.maxDrawdown < 0 ? 'loss' : ''} />
        <SummaryTile label="Best day" value={monthSummary.bestDay ? `${monthSummary.bestDay.label} ${fmtMoney(monthSummary.bestDay.pl)}` : 'N/A'} tone="gain" />
        <SummaryTile label="Worst day" value={monthSummary.worstDay ? `${monthSummary.worstDay.label} ${fmtMoney(monthSummary.worstDay.pl)}` : 'N/A'} tone="loss" />
      </div>
      <CumulativePlChart points={curvePoints} />
      <div className="calendar-month-grid">
        {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((label) => (
          <div key={label} className="calendar-weekday">{label}</div>
        ))}
        {monthCells.map((cell) => {
          const eventBadges = formatAutoEventBadges(cell.autoEvents);

          return (
            <button
              key={cell.key}
              type="button"
              className={`calendar-day-cell ${cell.inMonth ? '' : 'calendar-day-muted'} ${activeKey === cell.key ? 'calendar-day-active' : ''}`}
              onClick={() => onSelectDay(cell.key)}
            >
              <span className="calendar-day-number">{cell.date.getDate()}</span>
              {cell.tradeDay && (
                <span className={cell.tradeDay.pl >= 0 ? 'gain' : 'loss'}>{fmtMoney(cell.tradeDay.pl)}</span>
              )}
              {eventBadges.length > 0 && (
                <span className="calendar-day-badges">
                  {eventBadges.map((badge) => (
                    <span key={badge.type} className="calendar-day-badge">
                      {badge.label} {badge.count}
                    </span>
                  ))}
                </span>
              )}
              {hasReview(dayReviews[cell.key]) && <span className="calendar-day-note">review saved</span>}
              {planningEvents[cell.key]?.length > 0 && (
                <span className="calendar-day-note">{planningEvents[cell.key].length} planned</span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function CalendarSymbolStatsPanel({ rows, activeSymbol, onSelectSymbol }) {
  return (
    <div className="panel calendar-symbol-stats">
      <div className="calendar-symbol-stats-header">
        <div>
          <h3 className="page-title">Symbol Stats</h3>
          <p className="section-copy">Current month closed-trade performance.</p>
        </div>
        {activeSymbol !== 'All' && (
          <button type="button" className="scanner-button" onClick={() => onSelectSymbol('All')}>
            Clear
          </button>
        )}
      </div>
      {rows.length ? (
        <div className="calendar-symbol-stats-grid">
          {rows.slice(0, 8).map((row) => {
            const winRate = row.tradeCount ? (row.wins / row.tradeCount) * 100 : null;
            return (
              <button
                key={row.symbol}
                type="button"
                className={`calendar-symbol-stat ${activeSymbol === row.symbol ? 'calendar-symbol-stat-active' : ''}`}
                onClick={() => onSelectSymbol(row.symbol)}
              >
                <span className="calendar-symbol-stat-symbol">{row.symbol}</span>
                <strong className={row.pl >= 0 ? 'gain' : 'loss'}>{fmtMoney(row.pl)}</strong>
                <span className="muted-small">
                  {row.tradeCount} trades / {winRate === null ? 'N/A' : fmtPct(winRate)}
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="empty-panel">No closed trades for this month.</div>
      )}
    </div>
  );
}

export function CalendarDayList({
  bulletinQuery,
  onBulletinQueryChange,
  days,
  activeDay,
  newsCounts,
  dayReviews,
  planningEvents,
  onSelectDay,
  hasReview,
}) {
  return (
    <div className="calendar-list">
      <label className="calendar-search">
        <span>Search days</span>
        <input
          value={bulletinQuery}
          onChange={(event) => onBulletinQueryChange(event.target.value)}
          placeholder="AAPL, earnings, chased, FOMC"
        />
      </label>
      {days.map((day) => (
        <button
          key={day.key}
          type="button"
          className={`panel trade-day-button ${activeDay?.key === day.key ? 'trade-day-active' : ''}`}
          onClick={() => onSelectDay(day.key)}
        >
          <span className="calendar-time">{day.label}</span>
          <span className="trade-day-summary">
            {day.trades.length} trades • {day.volume.toLocaleString(undefined, { maximumFractionDigits: 4 })} shares
          </span>
          {newsCounts.get(day.key) > 0 && (
            <span className="trade-day-summary">{newsCounts.get(day.key)} auto events</span>
          )}
          {hasReview(dayReviews[day.key]) && (
            <span className="trade-day-summary">Review saved</span>
          )}
          {planningEvents[day.key]?.length > 0 && (
            <span className="trade-day-summary">{planningEvents[day.key].length} planned events</span>
          )}
          <span className={day.pl >= 0 ? 'gain' : 'loss'}>{fmtMoney(day.pl)}</span>
        </button>
      ))}
      {!days.length && (
        <div className="empty-panel">No calendar days match this search.</div>
      )}
    </div>
  );
}

export function MarketEventsPanel({ sections }) {
  const [activeTitle, setActiveTitle] = useState(sections[0]?.title || '');
  const activeSection = sections.find((section) => section.title === activeTitle) || sections[0];

  return (
    <div className="panel market-events-panel">
      <h3 className="page-title">Market Events</h3>
      <div className="market-event-tabs" role="tablist" aria-label="Market event categories">
        {sections.map((section) => {
          const selected = section.title === activeSection?.title;
          return (
            <button
              key={section.title}
              type="button"
              role="tab"
              aria-selected={selected}
              className={`market-event-tab ${selected ? 'market-event-tab-active' : ''}`}
              onClick={() => setActiveTitle(section.title)}
            >
              <span className="item-icon">{marketEventIcons[section.title] || <FiCalendar />}</span>
              <span>{section.title}</span>
              <span className="market-event-tab-count">{section.totalCount ?? section.articles.length}</span>
            </button>
          );
        })}
      </div>
      {activeSection?.title === 'Market Notes' ? (
        <div className="market-event-tab-panel" role="tabpanel">
          <MarketNotesPanel activeDay={null} activeNews={activeSection.articles} embedded />
        </div>
      ) : activeSection ? (
        <article className="market-event-section market-event-tab-panel" role="tabpanel">
          {activeSection.articles.length ? (
            <div className="calendar-event-links">
              {activeSection.articles.map((article, index) => (
                <a
                  key={`${article.url || article.title}-${index}`}
                  href={article.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="calendar-event-link"
                >
                  <span>{article.title || 'Untitled event'}</span>
                  <span className="muted-small">{article.source || 'Unknown source'}</span>
                </a>
              ))}
            </div>
          ) : (
            <div className="muted-list-empty">No events connected yet.</div>
          )}
        </article>
      ) : null}
    </div>
  );
}

export function MarketNotesPanel({ activeDay, activeNews, embedded = false }) {
  const [bucket, setBucket] = useState('all');
  const [query, setQuery] = useState('');
  const buckets = useMemo(() => {
    const counts = new Map();
    const add = (key, label, group) => {
      const current = counts.get(key) || { key, label, group, count: 0 };
      current.count += 1;
      counts.set(key, current);
    };
    for (const article of activeNews) {
      (article.tickers || []).forEach((ticker) => add(`ticker:${ticker}`, `$${ticker}`, 'Companies'));
      (article.related_companies || []).forEach((company) => {
        if (company?.name) add(`company:${company.name}`, company.name, 'Companies');
      });
      (article.related_commodities || []).forEach((commodity) => {
        add(`commodity:${commodity}`, commodity, 'Commodities');
      });
      (article.llm_topics || []).forEach((topic) => {
        add(`topic:${topic}`, topic, 'Topics');
      });
    }
    return Array.from(counts.values()).sort((left, right) => (
      left.group.localeCompare(right.group)
      || right.count - left.count
      || left.label.localeCompare(right.label)
    ));
  }, [activeNews]);
  const filteredNews = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return activeNews.filter((article) => {
      const keys = [
        ...(article.tickers || []).map((value) => `ticker:${value}`),
        ...(article.related_companies || []).filter((value) => value?.name).map((value) => `company:${value.name}`),
        ...(article.related_commodities || []).map((value) => `commodity:${value}`),
        ...(article.llm_topics || []).map((value) => `topic:${value}`),
      ];
      if (bucket !== 'all' && !keys.includes(bucket)) return false;
      if (!needle) return true;
      return [
        article.title,
        article.summary,
        article.source,
        ...(article.tickers || []),
        ...(article.related_companies || []).map((company) => company.name),
        ...(article.related_commodities || []),
        ...(article.llm_topics || []),
      ].filter(Boolean).join(' ').toLowerCase().includes(needle);
    });
  }, [activeNews, bucket, query]);

  return (
    <div className={`${embedded ? 'market-notes-embedded' : 'panel'} calendar-market-notes`}>
      <div>
        <h3 className="page-title">Market Notes {activeDay ? `for ${activeDay.label}` : ''}</h3>
        <p className="section-copy">AI-classified financial coverage with validated company tickers.</p>
      </div>
      <div className="market-note-controls">
        <label>
          <span>Bucket</span>
          <select
            aria-label="Market note bucket"
            value={bucket}
            onChange={(event) => setBucket(event.target.value)}
          >
            <option value="all">All analyzed ({activeNews.length})</option>
            {['Companies', 'Commodities', 'Topics'].map((group) => (
              <optgroup key={group} label={group}>
                {buckets.filter((item) => item.group === group).map((item) => (
                  <option key={item.key} value={item.key}>{item.label} ({item.count})</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label className="market-note-search">
          <span>Search</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Headline, company, ticker, topic"
          />
        </label>
        <span className="muted-small">{filteredNews.length} results</span>
      </div>
      {filteredNews.length ? (
        <div className="calendar-news-list">
          {filteredNews.slice(0, 100).map((article, index) => (
            <article
              key={`${article.url || article.title}-${index}`}
              className="calendar-news-item"
            >
              <div className="market-note-primary-labels">
                {(article.tickers || []).map((ticker) => (
                  <a key={ticker} href={`/scanner?symbol=${encodeURIComponent(ticker)}`} className="news-ticker">
                    {ticker}
                  </a>
                ))}
                {(article.related_commodities || []).map((commodity) => (
                  <span key={commodity} className="news-commodity">Commodity: {commodity}</span>
                ))}
                <span className="status-pill status-pill-ok">
                  AI analyzed {Math.round(Number(article.relevance_confidence || 0) * 100)}%
                </span>
              </div>
              <a href={article.url} target="_blank" rel="noopener noreferrer" className="calendar-news-title">
                {article.title || 'Untitled market note'}
              </a>
              <div className="market-note-labels">
                <span className="status-pill status-pill-muted">{article.relevance_label}</span>
                {(article.related_companies || []).slice(0, 4).map((company) => (
                  <span key={company.name} className="status-pill">Company: {company.name}</span>
                ))}
                {(article.llm_topics || []).slice(0, 5).map((topic) => (
                  <span key={topic} className="status-pill status-pill-muted">{topic}</span>
                ))}
              </div>
              {article.summary && <p className="market-note-summary">{article.summary}</p>}
              {(article.company_relationships || []).length > 0 && (
                <div className="market-note-relationships">
                  {article.company_relationships.slice(0, 3).map((relationship, relationshipIndex) => (
                    <span key={`${relationship.company_a}-${relationship.company_b}-${relationshipIndex}`}>
                      {relationship.company_a} / {relationship.relationship} / {relationship.company_b}
                    </span>
                  ))}
                </div>
              )}
              {(article.related_articles || []).length > 1 && (
                <details className="calendar-related-coverage">
                  <summary>Related coverage ({article.related_articles.length})</summary>
                  <div>
                    {article.related_articles.map((related) => (
                      <a
                        key={related.url}
                        href={related.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <strong>{related.source || related.provider || 'Related publisher'}</strong>
                        <span>{related.title || related.url}</span>
                      </a>
                    ))}
                  </div>
                </details>
              )}
              <span className="muted-small">
                {article.source || 'Unknown source'}
                {(article.source_urls || []).length > 1 ? ` / ${article.source_urls.length} related sources` : ''}
              </span>
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-panel">No analyzed market notes match this bucket.</div>
      )}
      {filteredNews.length > 100 && (
        <span className="muted-small">Showing the first 100 results. Narrow the bucket or search to continue.</span>
      )}
    </div>
  );
}

export function SummaryTile({ label, value, tone }) {
  return (
    <div className="calendar-summary-tile">
      <span className="muted-small">{label}</span>
      <strong className={tone || ''}>{value}</strong>
    </div>
  );
}

export function CumulativePlChart({ points }) {
  if (!points.length) {
    return <div className="empty-panel calendar-chart-empty">No closed trades in this month.</div>;
  }

  const width = 720;
  const height = 160;
  const padding = 20;
  const values = points.map((point) => point.value);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const usableWidth = width - padding * 2;
  const usableHeight = height - padding * 2;
  const xFor = (index) => padding + (points.length === 1 ? usableWidth : (index / (points.length - 1)) * usableWidth);
  const yFor = (value) => padding + ((max - value) / span) * usableHeight;
  const line = points.map((point, index) => `${xFor(index)},${yFor(point.value)}`).join(' ');
  const zeroY = yFor(0);
  const finalValue = points[points.length - 1].value;

  return (
    <div className="calendar-chart">
      <div className="calendar-chart-header">
        <span className="muted-small">Cumulative P/L</span>
        <strong className={finalValue >= 0 ? 'gain' : 'loss'}>{fmtMoney(finalValue)}</strong>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Cumulative monthly profit and loss">
        <line x1={padding} x2={width - padding} y1={zeroY} y2={zeroY} className="calendar-chart-zero" />
        <polyline points={line} className={finalValue >= 0 ? 'calendar-chart-line-gain' : 'calendar-chart-line-loss'} />
        {points.map((point, index) => (
          <circle
            key={point.key}
            cx={xFor(index)}
            cy={yFor(point.value)}
            r="4"
            className={point.value >= 0 ? 'calendar-chart-dot-gain' : 'calendar-chart-dot-loss'}
          />
        ))}
      </svg>
    </div>
  );
}
