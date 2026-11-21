import { useCallback } from 'react';
import { downloadTextFile } from '../lib/calendarFiles';
import {
  buildMonthReviewExport,
  monthFromKey,
  monthLabel,
  persistCalendarState,
} from '../lib/calendarUtils';

export function useCalendarArchiveActions({
  autoEventsByDay,
  dayReviews,
  days,
  monthSummary,
  planningEvents,
  reviewArchives,
  reviewQueue,
  reviewSummary,
  setCalendarSaveStatus,
  setCalendarUpdatedAt,
  setReviewArchives,
  setSelectedKey,
  setVisibleMonth,
  saveTimerRef,
  symbolStats,
  visibleMonth,
  visibleMonthKey,
}) {
  const buildVisibleMonthReview = useCallback(() => buildMonthReviewExport({
    visibleMonth,
    days,
    monthSummary,
    symbolStats,
    reviewSummary,
    reviewQueue,
    dayReviews,
    planningEvents,
    autoEventsByDay,
  }), [autoEventsByDay, dayReviews, days, monthSummary, planningEvents, reviewQueue, reviewSummary, symbolStats, visibleMonth]);

  const exportMonthReview = useCallback(() => {
    const text = buildVisibleMonthReview();
    downloadTextFile(text, `month-review-${visibleMonthKey}.txt`);
  }, [buildVisibleMonthReview, visibleMonthKey]);

  const archiveMonthReview = useCallback(() => {
    const text = buildVisibleMonthReview();
    const archives = {
      ...reviewArchives,
      [visibleMonthKey]: {
        month: visibleMonthKey,
        title: `${monthLabel(visibleMonth)} Trading Review`,
        text,
        created_at: Date.now(),
      },
    };
    setReviewArchives(archives);
    persistCalendarState(dayReviews, planningEvents, archives, setCalendarUpdatedAt, setCalendarSaveStatus, saveTimerRef, 0);
  }, [
    buildVisibleMonthReview,
    dayReviews,
    planningEvents,
    reviewArchives,
    saveTimerRef,
    setCalendarSaveStatus,
    setCalendarUpdatedAt,
    setReviewArchives,
    visibleMonth,
    visibleMonthKey,
  ]);

  const openArchiveMonth = useCallback((monthKey) => {
    setVisibleMonth(monthFromKey(`${monthKey}-01`));
    setSelectedKey(`${monthKey}-01`);
  }, [setSelectedKey, setVisibleMonth]);

  const downloadArchive = useCallback((archive) => {
    downloadTextFile(archive.text || '', `month-review-${archive.month}.txt`);
  }, []);

  return {
    archiveMonthReview,
    downloadArchive,
    exportMonthReview,
    openArchiveMonth,
  };
}
