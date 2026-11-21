import { fmtMoney } from '../../lib/calendarUtils';
import { SummaryTile } from './CalendarPanels';

export function CalendarReviewQueuePanel({ days, onSelectDay, onMarkReviewed }) {
  return (
    <div className="panel calendar-review-queue">
      <div className="calendar-symbol-stats-header">
        <div>
          <h3 className="page-title">Needs Review</h3>
          <p className="section-copy">Unreviewed month days with losses, activity, or events.</p>
        </div>
        <span className="calendar-filter-summary">{days.length.toLocaleString()} days</span>
      </div>
      {days.length ? (
        <div className="calendar-review-list">
          {days.slice(0, 6).map((day) => (
            <div key={day.key} className="calendar-review-item">
              <button
                type="button"
                className="calendar-review-jump"
                onClick={() => onSelectDay(day.key)}
              >
                <span className="calendar-time">{day.label}</span>
                <strong className={day.pl >= 0 ? 'gain' : 'loss'}>{fmtMoney(day.pl)}</strong>
                <span className="trade-day-summary">
                  {day.trades.length} trades / {day.autoEventCount + day.plannedEventCount} events
                </span>
                <span className="calendar-day-badges">
                  {day.reasons.map((reason) => (
                    <span key={reason} className="calendar-day-badge">{reason}</span>
                  ))}
                </span>
              </button>
              <button
                type="button"
                className="planning-delete-button calendar-review-mark"
                onClick={() => onMarkReviewed(day.key)}
              >
                Mark reviewed
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-panel">No priority review days for this month.</div>
      )}
    </div>
  );
}

export function CalendarReviewSummaryPanel({ summary }) {
  return (
    <div className="panel calendar-review-summary">
      <div>
        <h3 className="page-title">Review Summary</h3>
        <p className="section-copy">Current month journal coverage and recurring tags.</p>
      </div>
      <div className="calendar-summary-grid calendar-review-summary-grid">
        <SummaryTile label="Reviewed days" value={summary.reviewedDays.toLocaleString()} tone="gain" />
        <SummaryTile label="Notes written" value={summary.notedDays.toLocaleString()} />
        <SummaryTile label="Needs review" value={summary.needsReviewDays.toLocaleString()} tone={summary.needsReviewDays ? 'loss' : 'gain'} />
      </div>
      {summary.tagCounts.length ? (
        <div className="calendar-review-tags">
          {summary.tagCounts.slice(0, 8).map((item) => (
            <span key={item.tag} className="review-tag review-tag-active">
              {item.tag} {item.count}
            </span>
          ))}
        </div>
      ) : (
        <div className="empty-panel">No review tags for this month yet.</div>
      )}
    </div>
  );
}

export function CalendarMonthExportPanel({ archive, onArchive, onExport }) {
  return (
    <div className="panel calendar-month-export">
      <div>
        <h3 className="page-title">Month-End Export</h3>
        <p className="section-copy">
          {archive ? `Archived ${new Date(archive.created_at).toLocaleDateString()}` : 'Archive or download the visible month review.'}
        </p>
      </div>
      <div className="calendar-month-export-actions">
        <button type="button" className="scanner-button" onClick={onArchive}>
          Archive month
        </button>
        <button type="button" className="scanner-button" onClick={onExport}>
          Export month review
        </button>
      </div>
    </div>
  );
}

export function CalendarArchiveBrowserPanel({
  archives,
  query,
  onQueryChange,
  onOpenMonth,
  onDownloadArchive,
}) {
  const archiveList = Object.values(archives || {})
    .filter((archive) => {
      const needle = query.trim().toLowerCase();
      if (!needle) return true;
      return [
        archive.month,
        archive.title,
        archive.text,
      ].join(' ').toLowerCase().includes(needle);
    })
    .sort((a, b) => String(b.month).localeCompare(String(a.month)));

  return (
    <div className="panel calendar-archive-browser">
      <div className="calendar-symbol-stats-header">
        <div>
          <h3 className="page-title">Review Archives</h3>
          <p className="section-copy">Saved month-end review snapshots.</p>
        </div>
        <span className="calendar-filter-summary">{archiveList.length.toLocaleString()} saved</span>
      </div>
      <label className="calendar-search">
        <span>Search archives</span>
        <input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="2026-06, AAPL, chased, drawdown"
        />
      </label>
      {archiveList.length ? (
        <div className="calendar-archive-list">
          {archiveList.map((archive) => (
            <div key={archive.month} className="calendar-archive-item">
              <button
                type="button"
                className="calendar-review-jump"
                onClick={() => onOpenMonth(archive.month)}
              >
                <strong>{archive.title || `${archive.month} Trading Review`}</strong>
                <span className="muted-small">
                  {archive.month} / {new Date(archive.created_at).toLocaleDateString()}
                </span>
              </button>
              <button
                type="button"
                className="planning-delete-button"
                onClick={() => onDownloadArchive(archive)}
              >
                Download
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-panel">No archived reviews match this search.</div>
      )}
    </div>
  );
}
