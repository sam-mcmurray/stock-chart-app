import {
  mergePlanningEvents,
  normalizePlanningEvents,
  normalizeReviewArchives,
  normalizeReviews,
  todayKey,
} from './calendarUtils';

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export function downloadTextFile(text, filename) {
  downloadBlob(new Blob([text], { type: 'text/plain' }), filename);
}

export function downloadJsonFile(payload, filename) {
  downloadBlob(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }), filename);
}

export function exportCalendarStateFile(dayReviews, planningEvents, reviewArchives) {
  downloadJsonFile({
    schema_version: 1,
    exported_at: new Date().toISOString(),
    reviews: dayReviews,
    planning_events: planningEvents,
    review_archives: reviewArchives,
  }, `stock-calendar-${todayKey()}.json`);
}

export async function parseCalendarImportFile(file, currentReviews, currentPlanningEvents, currentArchives) {
  const parsed = JSON.parse(await file.text());
  const importedReviews = normalizeReviews(parsed?.reviews);
  const importedPlans = normalizePlanningEvents(parsed?.planning_events);
  const importedArchives = normalizeReviewArchives(parsed?.review_archives);

  return {
    reviews: { ...normalizeReviews(currentReviews), ...importedReviews },
    planningEvents: mergePlanningEvents(currentPlanningEvents, importedPlans),
    reviewArchives: { ...normalizeReviewArchives(currentArchives), ...importedArchives },
  };
}
