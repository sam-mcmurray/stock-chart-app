import { useMemo } from 'react';
import {
  buildAutoEventsByDay,
  buildBulletinDays,
  buildExtractedEventsByDay,
  buildMarketEventSections,
  buildMonthCells,
  buildReviewQueue,
  buildSymbolUniverse,
  countAutoEvents,
  countPlanningEvents,
  daySearchText,
  deduplicateCalendarNews,
  emptyReview,
  formatDay,
  groupTradesByDay,
  inferArticleEventKey,
  monthCurvePoints,
  normalizeReviews,
  summarizeMonth,
  summarizeReviewsForMonth,
  summarizeSymbolsForMonth,
  todayKey,
  persistentMarketNews,
} from '../lib/calendarUtils';

export function useCalendarDerivedData({
  autoEventFilter,
  bulletinQuery,
  dayReviews,
  news,
  newsEvents,
  planningEvents,
  reviewArchives,
  selectedKey,
  summary,
  symbolFilter,
  visibleMonth,
}) {
  const symbolUniverse = useMemo(() => buildSymbolUniverse(summary), [summary]);
  const calendarNews = useMemo(() => {
    const combined = [...(Array.isArray(news) ? news : []), ...persistentMarketNews(newsEvents)];
    return deduplicateCalendarNews(
      combined.filter((article) => ['finance', 'business', 'markets'].includes(article.relevance_label)),
    ).sort((left, right) => Number(right.published_ts || 0) - Number(left.published_ts || 0));
  }, [news, newsEvents]);
  const filteredTrades = useMemo(() => {
    const trades = Array.isArray(summary?.recent_trades) ? summary.recent_trades : [];
    if (symbolFilter === 'All') return trades;
    return trades.filter((trade) => String(trade?.symbol || '').toUpperCase() === symbolFilter);
  }, [summary, symbolFilter]);
  const days = useMemo(() => groupTradesByDay(filteredTrades), [filteredTrades]);
  const dayMap = useMemo(() => new Map(days.map((day) => [day.key, day])), [days]);
  const fallbackAutoEventsByDay = useMemo(() => buildAutoEventsByDay(news, symbolUniverse), [news, symbolUniverse]);
  const extractedAutoEventsByDay = useMemo(() => buildExtractedEventsByDay(newsEvents), [newsEvents]);
  const allAutoEventsByDay = newsEvents.length ? extractedAutoEventsByDay : fallbackAutoEventsByDay;
  const autoEventsByDay = useMemo(() => {
    if (symbolFilter === 'All') return allAutoEventsByDay;
    const filtered = new Map();
    for (const [key, events] of allAutoEventsByDay.entries()) {
      const matchingEvents = events.filter((event) => (event.symbols || []).includes(symbolFilter));
      if (matchingEvents.length) filtered.set(key, matchingEvents);
    }
    return filtered;
  }, [allAutoEventsByDay, symbolFilter]);
  const newsCounts = useMemo(() => {
    const counts = new Map();
    for (const [key, events] of autoEventsByDay.entries()) counts.set(key, events.length);
    return counts;
  }, [autoEventsByDay]);
  const activeKey = selectedKey || days[0]?.key || todayKey();
  const activeDay = useMemo(
    () => dayMap.get(activeKey) || {
      key: activeKey,
      label: formatDay(activeKey),
      trades: [],
      pl: 0,
      volume: 0,
    },
    [activeKey, dayMap],
  );
  const bulletinDays = useMemo(
    () => buildBulletinDays(days, planningEvents, dayReviews, newsCounts, activeKey),
    [activeKey, days, dayReviews, newsCounts, planningEvents],
  );
  const monthCells = useMemo(
    () => buildMonthCells(visibleMonth, dayMap, autoEventsByDay),
    [visibleMonth, dayMap, autoEventsByDay],
  );
  const monthSummary = useMemo(() => summarizeMonth(days, visibleMonth), [days, visibleMonth]);
  const curvePoints = useMemo(() => monthCurvePoints(days, visibleMonth), [days, visibleMonth]);
  const symbolStats = useMemo(() => summarizeSymbolsForMonth(days, visibleMonth), [days, visibleMonth]);
  const reviewQueue = useMemo(
    () => buildReviewQueue(days, visibleMonth, dayReviews, autoEventsByDay, planningEvents),
    [autoEventsByDay, dayReviews, days, planningEvents, visibleMonth],
  );
  const reviewSummary = useMemo(
    () => summarizeReviewsForMonth(days, visibleMonth, dayReviews, reviewQueue),
    [dayReviews, days, reviewQueue, visibleMonth],
  );
  const activeNews = useMemo(() => {
    if (!activeDay || activeDay.key === 'unscheduled') return [];
    return calendarNews
      .filter((article) => {
        if (inferArticleEventKey(article) !== activeDay.key) return false;
        if (symbolFilter === 'All') return true;
        const tickers = Array.isArray(article.tickers) ? article.tickers.map((ticker) => String(ticker).toUpperCase()) : [];
        const text = `${article.title || ''} ${article.summary || ''}`.toUpperCase();
        return tickers.includes(symbolFilter) || new RegExp(`(^|[^A-Z])${symbolFilter}([^A-Z]|$)`).test(text);
      })
      .slice(0, 200);
  }, [activeDay, calendarNews, symbolFilter]);
  const marketEventSections = useMemo(() => buildMarketEventSections(calendarNews), [calendarNews]);
  const activeReview = dayReviews[activeKey] || emptyReview();
  const activePlanningEvents = planningEvents[activeKey] || [];
  const activeAutoEvents = useMemo(
    () => autoEventsByDay.get(activeKey) || [],
    [activeKey, autoEventsByDay],
  );
  const filteredAutoEvents = autoEventFilter === 'All'
    ? activeAutoEvents
    : activeAutoEvents.filter((event) => event.type === autoEventFilter);
  const activeAutoEventCounts = useMemo(() => countAutoEvents(activeAutoEvents), [activeAutoEvents]);
  const reviewDayCount = useMemo(() => Object.keys(normalizeReviews(dayReviews)).length, [dayReviews]);
  const planningEventCount = useMemo(() => countPlanningEvents(planningEvents), [planningEvents]);
  const filteredBulletinDays = useMemo(() => {
    const query = bulletinQuery.trim().toLowerCase();
    if (!query) return bulletinDays;
    return bulletinDays.filter((day) => daySearchText(day, {
      dayReviews,
      planningEvents,
      autoEventsByDay,
      news: calendarNews,
    }).includes(query));
  }, [autoEventsByDay, bulletinDays, bulletinQuery, calendarNews, dayReviews, planningEvents]);
  const symbolFilterCount = symbolFilter === 'All'
    ? Array.isArray(summary?.recent_trades) ? summary.recent_trades.length : 0
    : filteredTrades.length;
  const visibleMonthKey = `${visibleMonth.getFullYear()}-${String(visibleMonth.getMonth() + 1).padStart(2, '0')}`;
  const visibleMonthArchive = reviewArchives[visibleMonthKey] || null;

  return {
    activeAutoEventCounts,
    activeAutoEvents,
    activeDay,
    activeKey,
    activeNews,
    activePlanningEvents,
    activeReview,
    autoEventsByDay,
    curvePoints,
    days,
    filteredAutoEvents,
    filteredBulletinDays,
    marketEventSections,
    monthCells,
    monthSummary,
    newsCounts,
    planningEventCount,
    reviewDayCount,
    reviewQueue,
    reviewSummary,
    symbolFilterCount,
    symbolStats,
    symbolUniverse,
    visibleMonthArchive,
    visibleMonthKey,
  };
}
