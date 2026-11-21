import { updateCalendarState } from './api';

export const CALENDAR_REVIEW_KEY = 'stock-chart-app:calendar-day-reviews';
export const CALENDAR_PLAN_KEY = 'stock-chart-app:calendar-planning-events';
export const CALENDAR_ARCHIVE_KEY = 'stock-chart-app:calendar-review-archives';

export function emptyReview() {
  return { notes: '', tags: [] };
}

export function hasReview(review) {
  return Boolean(review?.notes?.trim() || review?.tags?.length);
}

export function loadDayReviews() {
  if (typeof window === 'undefined') return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(CALENDAR_REVIEW_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function saveDayReviews(reviews) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(CALENDAR_REVIEW_KEY, JSON.stringify(reviews));
}

export function loadPlanningEvents() {
  if (typeof window === 'undefined') return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(CALENDAR_PLAN_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function savePlanningEvents(events) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(CALENDAR_PLAN_KEY, JSON.stringify(events));
}

export function loadReviewArchives() {
  if (typeof window === 'undefined') return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(CALENDAR_ARCHIVE_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function saveReviewArchives(archives) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(CALENDAR_ARCHIVE_KEY, JSON.stringify(archives));
}

export function persistCalendarState(reviews, planningEvents, reviewArchives, onSaved, onStatusChange, saveTimerRef, debounceMs = 500) {
  saveDayReviews(reviews);
  savePlanningEvents(planningEvents);
  saveReviewArchives(reviewArchives);
  onStatusChange?.('saving');
  if (saveTimerRef?.current) clearTimeout(saveTimerRef.current);

  const runSave = () => {
    updateCalendarState({ reviews, planning_events: planningEvents, review_archives: reviewArchives })
      .then((state) => {
        if (Number.isFinite(state?.updated_at)) onSaved?.(state.updated_at);
        onStatusChange?.('saved');
      })
      .catch(() => onStatusChange?.('local'));
  };

  if (debounceMs <= 0) {
    runSave();
    return;
  }
  if (saveTimerRef) saveTimerRef.current = setTimeout(runSave, debounceMs);
}

export function normalizeReviews(reviews) {
  const normalized = {};
  for (const [key, review] of Object.entries(reviews || {})) {
    if (!key || typeof review !== 'object' || !review) continue;
    const nextReview = {
      notes: typeof review.notes === 'string' ? review.notes : '',
      tags: Array.isArray(review.tags) ? review.tags.map(String).filter(Boolean) : [],
    };
    if (hasReview(nextReview)) normalized[key] = nextReview;
  }
  return normalized;
}

export function normalizePlanningEvents(planningEvents) {
  const normalized = {};
  for (const [key, events] of Object.entries(planningEvents || {})) {
    const validEvents = (Array.isArray(events) ? events : [])
      .map((event) => {
        if (!event || typeof event !== 'object' || !event.id || !event.title) return null;
        return {
          id: String(event.id),
          type: event.type ? String(event.type) : 'Personal',
          title: String(event.title),
          note: event.note ? String(event.note) : '',
        };
      })
      .filter(Boolean);
    if (validEvents.length) normalized[key] = validEvents;
  }
  return normalized;
}

export function mergePlanningEvents(currentEvents, importedEvents) {
  const merged = normalizePlanningEvents(currentEvents);
  const normalizedImports = normalizePlanningEvents(importedEvents);
  for (const [key, events] of Object.entries(normalizedImports)) {
    const existing = Array.isArray(merged[key]) ? merged[key] : [];
    const seen = new Set(existing.map((event) => event.id));
    const additions = events.filter((event) => {
      if (!event?.id || seen.has(event.id)) return false;
      seen.add(event.id);
      return true;
    });
    merged[key] = [...existing, ...additions];
  }
  return merged;
}

export function normalizeReviewArchives(archives) {
  const normalized = {};
  for (const [month, archive] of Object.entries(archives || {})) {
    if (!month || typeof archive !== 'object' || !archive) continue;
    const text = typeof archive.text === 'string' ? archive.text : '';
    if (!text.trim()) continue;
    normalized[month] = {
      month: archive.month ? String(archive.month) : month,
      title: archive.title ? String(archive.title) : `${month} Trading Review`,
      text,
      created_at: Number.isFinite(archive.created_at) ? archive.created_at : Date.now(),
    };
  }
  return normalized;
}

export function formatSavedAt(timestamp, status) {
  if (status === 'saving') return 'Saving...';
  if (status === 'local') return 'Saved locally';
  if (!Number.isFinite(timestamp)) return 'Local fallback';
  return `Saved ${new Date(timestamp).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
}

export function countPlanningEvents(planningEvents) {
  return Object.values(planningEvents || {}).reduce((total, events) => total + (Array.isArray(events) ? events.length : 0), 0);
}
