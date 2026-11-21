import { useCallback } from 'react';
import {
  emptyReview,
  hasReview,
  persistCalendarState,
} from '../lib/calendarUtils';

export function useCalendarJournalState({
  activeKey,
  dayReviews,
  planningEvents,
  reviewArchives,
  setCalendarSaveStatus,
  setCalendarUpdatedAt,
  setDayReviews,
  setPlanningEvents,
  saveTimerRef,
}) {
  const updateActiveReview = useCallback((updater) => {
    setDayReviews((currentReviews) => {
      const currentReview = currentReviews[activeKey] || emptyReview();
      const nextReview = typeof updater === 'function'
        ? updater(currentReview)
        : { ...currentReview, ...updater };
      const normalizedReview = {
        notes: nextReview.notes || '',
        tags: Array.isArray(nextReview.tags) ? nextReview.tags : [],
      };
      const nextReviews = { ...currentReviews };

      if (hasReview(normalizedReview)) {
        nextReviews[activeKey] = normalizedReview;
      } else {
        delete nextReviews[activeKey];
      }

      persistCalendarState(nextReviews, planningEvents, reviewArchives, setCalendarUpdatedAt, setCalendarSaveStatus, saveTimerRef);
      return nextReviews;
    });
  }, [activeKey, planningEvents, reviewArchives, saveTimerRef, setCalendarSaveStatus, setCalendarUpdatedAt, setDayReviews]);

  const updateDayReview = useCallback((dayKey, updater) => {
    setDayReviews((currentReviews) => {
      const currentReview = currentReviews[dayKey] || emptyReview();
      const nextReview = typeof updater === 'function'
        ? updater(currentReview)
        : { ...currentReview, ...updater };
      const normalizedReview = {
        notes: nextReview.notes || '',
        tags: Array.isArray(nextReview.tags) ? nextReview.tags : [],
      };
      const nextReviews = { ...currentReviews };

      if (hasReview(normalizedReview)) {
        nextReviews[dayKey] = normalizedReview;
      } else {
        delete nextReviews[dayKey];
      }

      persistCalendarState(nextReviews, planningEvents, reviewArchives, setCalendarUpdatedAt, setCalendarSaveStatus, saveTimerRef);
      return nextReviews;
    });
  }, [planningEvents, reviewArchives, saveTimerRef, setCalendarSaveStatus, setCalendarUpdatedAt, setDayReviews]);

  const updateActiveReviewNotes = useCallback((notes) => {
    updateActiveReview({ notes });
  }, [updateActiveReview]);

  const toggleActiveReviewTag = useCallback((tag) => {
    updateActiveReview((review) => {
      const currentTags = Array.isArray(review.tags) ? review.tags : [];
      const tags = currentTags.includes(tag)
        ? currentTags.filter((item) => item !== tag)
        : [...currentTags, tag];
      return { ...review, tags };
    });
  }, [updateActiveReview]);

  const markDayReviewed = useCallback((dayKey) => {
    updateDayReview(dayKey, (review) => {
      const currentTags = Array.isArray(review.tags) ? review.tags : [];
      const tags = currentTags.includes('Reviewed')
        ? currentTags
        : [...currentTags, 'Reviewed'];
      return { ...review, tags };
    });
  }, [updateDayReview]);

  const addPlanningEvent = useCallback((event) => {
    setPlanningEvents((currentEvents) => {
      const nextEvents = {
        ...currentEvents,
        [activeKey]: [
          ...(currentEvents[activeKey] || []),
          {
            id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
            type: event.type || 'Personal',
            title: event.title.trim(),
            note: event.note.trim(),
          },
        ],
      };
      persistCalendarState(dayReviews, nextEvents, reviewArchives, setCalendarUpdatedAt, setCalendarSaveStatus, saveTimerRef);
      return nextEvents;
    });
  }, [activeKey, dayReviews, reviewArchives, saveTimerRef, setCalendarSaveStatus, setCalendarUpdatedAt, setPlanningEvents]);

  const deletePlanningEvent = useCallback((eventId) => {
    setPlanningEvents((currentEvents) => {
      const nextDayEvents = (currentEvents[activeKey] || []).filter((event) => event.id !== eventId);
      const nextEvents = { ...currentEvents };

      if (nextDayEvents.length) {
        nextEvents[activeKey] = nextDayEvents;
      } else {
        delete nextEvents[activeKey];
      }

      persistCalendarState(dayReviews, nextEvents, reviewArchives, setCalendarUpdatedAt, setCalendarSaveStatus, saveTimerRef);
      return nextEvents;
    });
  }, [activeKey, dayReviews, reviewArchives, saveTimerRef, setCalendarSaveStatus, setCalendarUpdatedAt, setPlanningEvents]);

  const updatePlanningEvent = useCallback((eventId, updates) => {
    setPlanningEvents((currentEvents) => {
      const nextDayEvents = (currentEvents[activeKey] || [])
        .map((event) => {
          if (event.id !== eventId) return event;
          return {
            ...event,
            type: updates.type || event.type || 'Personal',
            title: updates.title.trim(),
            note: updates.note.trim(),
          };
        })
        .filter((event) => event.title);
      const nextEvents = { ...currentEvents };

      if (nextDayEvents.length) {
        nextEvents[activeKey] = nextDayEvents;
      } else {
        delete nextEvents[activeKey];
      }

      persistCalendarState(dayReviews, nextEvents, reviewArchives, setCalendarUpdatedAt, setCalendarSaveStatus, saveTimerRef);
      return nextEvents;
    });
  }, [activeKey, dayReviews, reviewArchives, saveTimerRef, setCalendarSaveStatus, setCalendarUpdatedAt, setPlanningEvents]);

  return {
    addPlanningEvent,
    deletePlanningEvent,
    markDayReviewed,
    toggleActiveReviewTag,
    updateActiveReviewNotes,
    updatePlanningEvent,
  };
}
