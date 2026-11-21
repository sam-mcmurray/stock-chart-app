import { getCalendarState, getDashboardSummary, getNewsEvents, getRecentNews } from './api';
import {
  epochDaysAgo,
  normalizePlanningEvents,
  normalizeReviewArchives,
  normalizeReviews,
  saveDayReviews,
  savePlanningEvents,
  saveReviewArchives,
} from './calendarUtils';

export async function loadCalendarData() {
  const now = new Date();
  const monthStart = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`;
  const monthEnd = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()).padStart(2, '0')}`;
  const [summary, recentNews] = await Promise.all([
    getDashboardSummary({ since: epochDaysAgo(365), tradeLimit: 500 }),
    getRecentNews({ limit: 50 }).catch(() => []),
  ]);
  const [extractedEvents, calendarState] = await Promise.all([
    getNewsEvents({ limit: 10000, startDate: monthStart, endDate: monthEnd }).catch(() => []),
    getCalendarState().catch(() => null),
  ]);

  const result = {
    summary,
    news: Array.isArray(recentNews) ? recentNews : [],
    newsEvents: Array.isArray(extractedEvents) ? extractedEvents : [],
    reviews: null,
    planningEvents: null,
    reviewArchives: null,
    updatedAt: null,
    saveStatus: 'local',
  };

  if (calendarState && typeof calendarState === 'object') {
    result.reviews = normalizeReviews(calendarState.reviews);
    result.planningEvents = normalizePlanningEvents(calendarState.planning_events);
    result.reviewArchives = normalizeReviewArchives(calendarState.review_archives);
    result.updatedAt = Number.isFinite(calendarState.updated_at) ? calendarState.updated_at : null;
    result.saveStatus = Number.isFinite(calendarState.updated_at) ? 'saved' : 'local';
    saveDayReviews(result.reviews);
    savePlanningEvents(result.planningEvents);
    saveReviewArchives(result.reviewArchives);
  }

  return result;
}
