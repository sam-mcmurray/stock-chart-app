import { useCallback, useEffect, useRef, useState } from 'react';
import { loadCalendarData } from '../lib/calendarData';
import { getNewsEvents } from '../lib/api';
import {
  exportCalendarStateFile,
  parseCalendarImportFile,
} from '../lib/calendarFiles';
import {
  hasReview,
  loadDayReviews,
  loadPlanningEvents,
  loadReviewArchives,
  monthFromKey,
  persistCalendarState,
  shiftMonth,
  todayKey,
} from '../lib/calendarUtils';
import { useCalendarArchiveActions } from './useCalendarArchiveActions';
import { useCalendarDerivedData } from './useCalendarDerivedData';
import { useCalendarJournalState } from './useCalendarJournalState';

export function useCalendarController() {
  const [summary, setSummary] = useState(null);
  const [news, setNews] = useState([]);
  const [newsEvents, setNewsEvents] = useState([]);
  const [selectedKey, setSelectedKey] = useState('');
  const [visibleMonth, setVisibleMonth] = useState(() => monthFromKey(todayKey()));
  const [dayReviews, setDayReviews] = useState(loadDayReviews);
  const [planningEvents, setPlanningEvents] = useState(loadPlanningEvents);
  const [reviewArchives, setReviewArchives] = useState(loadReviewArchives);
  const [calendarUpdatedAt, setCalendarUpdatedAt] = useState(null);
  const [calendarSaveStatus, setCalendarSaveStatus] = useState('local');
  const [autoEventFilter, setAutoEventFilter] = useState('All');
  const [bulletinQuery, setBulletinQuery] = useState('');
  const [archiveQuery, setArchiveQuery] = useState('');
  const [symbolFilter, setSymbolFilter] = useState('All');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const calendarSaveTimerRef = useRef(null);
  const importInputRef = useRef(null);

  const loadCalendar = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await loadCalendarData();
      setSummary(data.summary);
      setNews(data.news);
      setNewsEvents(data.newsEvents);
      if (data.reviews && data.planningEvents && data.reviewArchives) {
        setDayReviews(data.reviews);
        setPlanningEvents(data.planningEvents);
        setReviewArchives(data.reviewArchives);
        setCalendarUpdatedAt(data.updatedAt);
        setCalendarSaveStatus(data.saveStatus);
      }
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCalendar();
  }, [loadCalendar]);

  useEffect(() => {
    const year = visibleMonth.getFullYear();
    const month = visibleMonth.getMonth();
    const startDate = `${year}-${String(month + 1).padStart(2, '0')}-01`;
    const endDate = `${year}-${String(month + 1).padStart(2, '0')}-${String(new Date(year, month + 1, 0).getDate()).padStart(2, '0')}`;
    let cancelled = false;
    getNewsEvents({ limit: 10000, startDate, endDate, includeGenerated: false })
      .then((events) => {
        if (cancelled || !Array.isArray(events)) return;
        setNewsEvents((current) => {
          const merged = new Map([...(current || []), ...events].map((event) => [event.id, event]));
          return Array.from(merged.values());
        });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [visibleMonth]);

  useEffect(() => () => {
    if (calendarSaveTimerRef.current) {
      clearTimeout(calendarSaveTimerRef.current);
    }
  }, []);

  useEffect(() => {
    if (calendarSaveStatus !== 'saving') return undefined;

    const warnBeforeUnload = (event) => {
      event.preventDefault();
      event.returnValue = '';
      return '';
    };

    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  }, [calendarSaveStatus]);

  const {
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
  } = useCalendarDerivedData({
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
  });

  const {
    addPlanningEvent,
    deletePlanningEvent,
    markDayReviewed,
    toggleActiveReviewTag,
    updateActiveReviewNotes,
    updatePlanningEvent,
  } = useCalendarJournalState({
    activeKey,
    dayReviews,
    planningEvents,
    reviewArchives,
    setCalendarSaveStatus,
    setCalendarUpdatedAt,
    setDayReviews,
    setPlanningEvents,
    saveTimerRef: calendarSaveTimerRef,
  });

  useEffect(() => {
    if (!selectedKey && days[0]) {
      setSelectedKey(days[0].key);
      setVisibleMonth(monthFromKey(days[0].key));
    }
  }, [days, selectedKey]);

  const selectCalendarDay = useCallback((key) => {
    setSelectedKey(key);
    setVisibleMonth(monthFromKey(key));
  }, []);

  const shiftSelectedDay = useCallback((delta) => {
    const baseKey = activeKey === 'unscheduled' ? todayKey() : activeKey;
    const [year, month, day] = baseKey.split('-').map(Number);
    const nextDate = new Date(year, month - 1, day);
    nextDate.setDate(nextDate.getDate() + delta);
    const nextKey = [
      nextDate.getFullYear(),
      String(nextDate.getMonth() + 1).padStart(2, '0'),
      String(nextDate.getDate()).padStart(2, '0'),
    ].join('-');
    selectCalendarDay(nextKey);
  }, [activeKey, selectCalendarDay]);

  const saveCalendarNow = useCallback(() => {
    persistCalendarState(dayReviews, planningEvents, reviewArchives, setCalendarUpdatedAt, setCalendarSaveStatus, calendarSaveTimerRef, 0);
  }, [dayReviews, planningEvents, reviewArchives]);

  const exportCalendarState = useCallback(() => {
    exportCalendarStateFile(dayReviews, planningEvents, reviewArchives);
  }, [dayReviews, planningEvents, reviewArchives]);

  const importCalendarState = useCallback(async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;

    try {
      const {
        reviews,
        planningEvents: plans,
        reviewArchives: archives,
      } = await parseCalendarImportFile(file, dayReviews, planningEvents, reviewArchives);
      setDayReviews(reviews);
      setPlanningEvents(plans);
      setReviewArchives(archives);
      persistCalendarState(reviews, plans, archives, setCalendarUpdatedAt, setCalendarSaveStatus, calendarSaveTimerRef, 0);
    } catch {
      setCalendarSaveStatus('local');
    }
  }, [dayReviews, planningEvents, reviewArchives]);

  const {
    archiveMonthReview,
    downloadArchive,
    exportMonthReview,
    openArchiveMonth,
  } = useCalendarArchiveActions({
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
    saveTimerRef: calendarSaveTimerRef,
    symbolStats,
    visibleMonth,
    visibleMonthKey,
  });

  const selectPreviousMonth = useCallback(() => {
    setVisibleMonth((current) => shiftMonth(current, -1));
  }, []);

  const selectNextMonth = useCallback(() => {
    setVisibleMonth((current) => shiftMonth(current, 1));
  }, []);

  const selectToday = useCallback(() => {
    const key = todayKey();
    setSelectedKey(key);
    setVisibleMonth(monthFromKey(key));
  }, []);

  return {
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
  };
}
