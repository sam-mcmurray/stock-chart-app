import { useEffect, useState } from 'react';
import { FiCalendar, FiRefreshCw } from 'react-icons/fi';
import {
  CalendarDayList,
  CalendarDayNavigator,
  CalendarMonthPanel,
  CalendarSymbolStatsPanel,
  CalendarSymbolFilter,
  MarketEventsPanel,
  MarketNotesPanel,
} from '../components/calendar/CalendarPanels';
import {
  AutoEventsPanel,
  DailyBulletinPanel,
  DayReviewPanel,
  EarningsEventsPanel,
  PlanningEventsPanel,
} from '../components/calendar/CalendarDetailPanels';
import { TradeDayDetail } from '../components/calendar/CalendarTradePanels';
import {
  CalendarArchiveBrowserPanel,
  CalendarMonthExportPanel,
  CalendarReviewQueuePanel,
  CalendarReviewSummaryPanel,
} from '../components/calendar/CalendarReviewPanels';
import { formatSavedAt, monthLabel } from '../lib/calendarUtils';
import { useCalendarController } from '../hooks/useCalendarController';

export default function Calendar(){
  const [activeDayTab, setActiveDayTab] = useState('bulletin');
  const [activeDetailView, setActiveDetailView] = useState('trades');
  const {
    activeAutoEventCounts,
    activeAutoEvents,
    activeDay,
    activeKey,
    activeNews,
    activePlanningEvents,
    activeReview,
    archiveMonthReview,
    archiveQuery,
    autoEventFilter,
    bulletinQuery,
    calendarSaveStatus,
    calendarUpdatedAt,
    curvePoints,
    dayReviews,
    deletePlanningEvent,
    downloadArchive,
    error,
    exportCalendarState,
    exportMonthReview,
    filteredAutoEvents,
    filteredBulletinDays,
    hasReview,
    importCalendarState,
    importInputRef,
    loadCalendar,
    loading,
    marketEventSections,
    monthCells,
    monthSummary,
    newsCounts,
    openArchiveMonth,
    planningEventCount,
    planningEvents,
    reviewArchives,
    reviewDayCount,
    reviewQueue,
    reviewSummary,
    saveCalendarNow,
    selectCalendarDay,
    selectNextMonth,
    selectPreviousMonth,
    selectToday,
    setArchiveQuery,
    setAutoEventFilter,
    setBulletinQuery,
    setSymbolFilter,
    shiftSelectedDay,
    summary,
    symbolFilter,
    symbolFilterCount,
    symbolStats,
    symbolUniverse,
    updateActiveReviewNotes,
    updatePlanningEvent,
    toggleActiveReviewTag,
    addPlanningEvent,
    markDayReviewed,
    visibleMonth,
    visibleMonthArchive,
  } = useCalendarController();
  const earningsCount = activeAutoEvents.filter(
    (event) => event.extractionSource === 'yahoo_earnings',
  ).length;
  const otherAutoEventCount = activeAutoEvents.length - earningsCount;
  const dayTabs = [
    { id: 'bulletin', label: 'Bulletin' },
    { id: 'earnings', label: 'Earnings', count: earningsCount, disabled: earningsCount === 0 },
    { id: 'details', label: 'Details' },
  ];
  const detailViews = [
    { id: 'trades', label: 'Trades', count: activeDay.trades.length },
    { id: 'news', label: 'News', count: activeNews.length },
    { id: 'planning', label: 'Planning', count: activePlanningEvents.length },
    { id: 'review', label: 'Review notes' },
    { id: 'events', label: 'Other events', count: otherAutoEventCount },
  ];

  useEffect(() => {
    if (activeDayTab === 'earnings' && earningsCount === 0) {
      setActiveDayTab('bulletin');
    }
  }, [activeDayTab, earningsCount]);

  return (
    <section className="page-stack">
      <div className="panel profile-header">
        <div>
          <h2 className="page-title"><FiCalendar /> Calendar</h2>
          <p className="section-copy">Daily trade P/L bulletins and closed-trade detail.</p>
          <p className={`calendar-sync-status calendar-sync-${calendarSaveStatus}`}>
            {formatSavedAt(calendarUpdatedAt, calendarSaveStatus)}
          </p>
          <p className="calendar-state-counts">
            {reviewDayCount} review days / {planningEventCount} planned events
          </p>
        </div>
        <div className="calendar-header-actions">
          <button
            type="button"
            className="scanner-button"
            onClick={exportCalendarState}
          >
            Export
          </button>
          <button
            type="button"
            className="scanner-button"
            onClick={() => importInputRef.current?.click()}
          >
            Import
          </button>
          <input
            ref={importInputRef}
            type="file"
            accept="application/json,.json"
            className="calendar-import-input"
            onChange={importCalendarState}
          />
          <button
            type="button"
            className="scanner-button"
            onClick={saveCalendarNow}
            disabled={calendarSaveStatus === 'saving'}
          >
            Save now
          </button>
          <button
            type="button"
            className="scanner-button"
            onClick={loadCalendar}
            disabled={loading}
          >
            <FiRefreshCw /> {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && <div className="panel state-error">Failed to load trade calendar: {error}</div>}

      {loading && !summary ? (
        <div className="panel">Loading trade calendar...</div>
      ) : (
        <>
          <CalendarSymbolFilter
            symbols={symbolUniverse}
            value={symbolFilter}
            tradeCount={symbolFilterCount}
            onChange={setSymbolFilter}
          />
          <CalendarMonthPanel
            visibleMonthLabel={monthLabel(visibleMonth)}
            monthSummary={monthSummary}
            curvePoints={curvePoints}
            monthCells={monthCells}
            activeKey={activeKey}
            dayReviews={dayReviews}
            planningEvents={planningEvents}
            onPreviousMonth={selectPreviousMonth}
            onNextMonth={selectNextMonth}
            onToday={selectToday}
            onSelectDay={selectCalendarDay}
            hasReview={hasReview}
          />
          <CalendarSymbolStatsPanel
            rows={symbolStats}
            activeSymbol={symbolFilter}
            onSelectSymbol={setSymbolFilter}
          />
          <CalendarReviewQueuePanel
            days={reviewQueue}
            onSelectDay={selectCalendarDay}
            onMarkReviewed={markDayReviewed}
          />
          <CalendarReviewSummaryPanel summary={reviewSummary} />
          <CalendarMonthExportPanel
            archive={visibleMonthArchive}
            onArchive={archiveMonthReview}
            onExport={exportMonthReview}
          />
          <CalendarArchiveBrowserPanel
            archives={reviewArchives}
            query={archiveQuery}
            onQueryChange={setArchiveQuery}
            onOpenMonth={openArchiveMonth}
            onDownloadArchive={downloadArchive}
          />

          <div className="trade-calendar-layout">
            <CalendarDayList
              bulletinQuery={bulletinQuery}
              onBulletinQueryChange={setBulletinQuery}
              days={filteredBulletinDays}
              activeDay={activeDay}
              newsCounts={newsCounts}
              dayReviews={dayReviews}
              planningEvents={planningEvents}
              onSelectDay={selectCalendarDay}
              hasReview={hasReview}
            />

            <div className="trade-detail-stack">
              <CalendarDayNavigator
                activeDay={activeDay}
                onPreviousDay={() => shiftSelectedDay(-1)}
                onNextDay={() => shiftSelectedDay(1)}
                onJumpToDay={selectCalendarDay}
              />
              <div className="calendar-day-tabs" role="tablist" aria-label="Selected day details">
                {dayTabs.map((tab) => (
                  <button
                    key={tab.id}
                    type="button"
                    role="tab"
                    aria-selected={activeDayTab === tab.id}
                    aria-controls={`calendar-day-panel-${tab.id}`}
                    disabled={tab.disabled}
                    className={`calendar-day-tab ${activeDayTab === tab.id ? 'calendar-day-tab-active' : ''}`}
                    onClick={() => setActiveDayTab(tab.id)}
                  >
                    <span>{tab.label}</span>
                    {Number.isFinite(tab.count) && <span className="calendar-day-tab-count">{tab.count}</span>}
                  </button>
                ))}
              </div>

              <div id={`calendar-day-panel-${activeDayTab}`} role="tabpanel">
                {activeDayTab === 'bulletin' && (
                  <DailyBulletinPanel
                    activeDay={activeDay}
                    review={activeReview}
                    planningEvents={activePlanningEvents}
                    autoEvents={activeAutoEvents}
                    marketNotes={activeNews}
                  />
                )}
                {activeDayTab === 'earnings' && (
                  <EarningsEventsPanel
                    activeDay={activeDay}
                    events={activeAutoEvents}
                    articles={activeNews}
                  />
                )}
                {activeDayTab === 'details' && (
                  <div className="calendar-detail-view">
                    <label className="calendar-detail-selector">
                      <span>View</span>
                      <select value={activeDetailView} onChange={(event) => setActiveDetailView(event.target.value)}>
                        {detailViews.map((view) => (
                          <option key={view.id} value={view.id}>
                            {view.label}{Number.isFinite(view.count) ? ` (${view.count})` : ''}
                          </option>
                        ))}
                      </select>
                    </label>
                    {activeDetailView === 'trades' && <TradeDayDetail activeDay={activeDay} />}
                    {activeDetailView === 'news' && (
                      <MarketNotesPanel activeDay={activeDay} activeNews={activeNews} />
                    )}
                    {activeDetailView === 'review' && (
                      <DayReviewPanel
                        activeDay={activeDay}
                        review={activeReview}
                        onNotesChange={updateActiveReviewNotes}
                        onToggleTag={toggleActiveReviewTag}
                      />
                    )}
                    {activeDetailView === 'planning' && (
                      <PlanningEventsPanel
                        activeDay={activeDay}
                        events={activePlanningEvents}
                        onAddEvent={addPlanningEvent}
                        onUpdateEvent={updatePlanningEvent}
                        onDeleteEvent={deletePlanningEvent}
                      />
                    )}
                    {activeDetailView === 'events' && (
                      <AutoEventsPanel
                        activeDay={activeDay}
                        events={filteredAutoEvents.filter((event) => event.extractionSource !== 'yahoo_earnings')}
                        counts={activeAutoEventCounts}
                        filter={autoEventFilter}
                        onFilterChange={setAutoEventFilter}
                      />
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </>
      )}

      <MarketEventsPanel sections={marketEventSections} />
    </section>
  );
}
