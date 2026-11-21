import { emptyReview, hasReview } from './calendarPersistence';
export {
  articleText,
  autoEventFilters,
  buildAutoEventsByDay,
  buildExtractedEventsByDay,
  buildMarketEventSections,
  buildSymbolUniverse,
  classifyAutoEvent,
  confidenceTone,
  countAutoEvents,
  daySearchText,
  deduplicateCalendarNews,
  formatAutoEventBadges,
  inferArticleEventKey,
  marketEventDefinitions,
  persistentMarketNews,
} from './calendarEvents';
export {
  CALENDAR_ARCHIVE_KEY,
  CALENDAR_PLAN_KEY,
  CALENDAR_REVIEW_KEY,
  countPlanningEvents,
  emptyReview,
  formatSavedAt,
  hasReview,
  loadDayReviews,
  loadPlanningEvents,
  loadReviewArchives,
  mergePlanningEvents,
  normalizePlanningEvents,
  normalizeReviewArchives,
  normalizeReviews,
  persistCalendarState,
  saveDayReviews,
  savePlanningEvents,
  saveReviewArchives,
} from './calendarPersistence';

export const reviewTagOptions = [
  'Reviewed',
  'Followed plan',
  'Chased entry',
  'Bad entry',
  'Respected stop',
  'Held too long',
  'News catalyst',
  'Needs review',
];

export const planEventTypes = ['Economic release', 'Earnings date', 'IPO watch', 'Personal', 'Risk'];

export const fmtMoney = (n) =>
  (n ?? 0) >= 0
    ? `$${(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
    : `-$${Math.abs(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

export const fmtPct = (n) =>
  Number.isFinite(n) ? `${n >= 0 ? '+' : ''}${n.toFixed(2)}%` : 'N/A';

const padDatePart = (value) => String(value).padStart(2, '0');

function keyFromDateParts(year, month, day) {
  return `${year}-${padDatePart(month)}-${padDatePart(day)}`;
}

function localDateKey(date) {
  if (!date || Number.isNaN(date.getTime())) return 'unscheduled';
  return keyFromDateParts(date.getFullYear(), date.getMonth() + 1, date.getDate());
}

export function tradeReturnPct(trade) {
  const entry = Number(trade?.entry || 0);
  const exit = Number(trade?.exit || 0);
  if (!entry || !exit) return null;
  return ((exit - entry) / entry) * 100;
}

export function timestampDateKey(timestamp) {
  if (!timestamp) return 'unscheduled';
  const date = new Date(Number(timestamp) * 1000);
  return localDateKey(date);
}

export function dateToKey(date) {
  return localDateKey(date);
}

export function formatDay(key) {
  if (key === 'unscheduled') return 'Unscheduled';
  const [year, month, day] = key.split('-').map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}

export function todayKey() {
  return localDateKey(new Date());
}

export function epochDaysAgo(days) {
  return Math.floor((Date.now() - days * 24 * 60 * 60 * 1000) / 1000);
}

export function monthLabel(monthDate) {
  return monthDate.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}

export function monthFromKey(key) {
  const safeKey = key === 'unscheduled' ? todayKey() : key;
  const [year, month] = safeKey.split('-').map(Number);
  return new Date(year, month - 1, 1);
}

export function shiftMonth(monthDate, delta) {
  return new Date(monthDate.getFullYear(), monthDate.getMonth() + delta, 1);
}

function tradeDateKey(trade) {
  if (!trade?.ts) return 'unscheduled';
  const date = new Date(Number(trade.ts) * 1000);
  return localDateKey(date);
}

export function buildMonthCells(monthDate, tradeMap, autoEventsByDay) {
  const year = monthDate.getFullYear();
  const month = monthDate.getMonth();
  const first = new Date(year, month, 1);
  const start = new Date(first);
  start.setDate(first.getDate() - first.getDay());

  return Array.from({ length: 42 }).map((_, index) => {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    const key = localDateKey(date);
    return {
      key,
      date,
      inMonth: date.getMonth() === month,
      tradeDay: tradeMap.get(key),
      autoEvents: autoEventsByDay.get(key) || [],
    };
  });
}

export function groupTradesByDay(trades) {
  const groups = new Map();
  for (const trade of Array.isArray(trades) ? trades : []) {
    const key = tradeDateKey(trade);
    const group = groups.get(key) || { key, label: formatDay(key), trades: [], pl: 0, volume: 0 };
    group.trades.push(trade);
    group.pl += Number(trade.pl || 0);
    group.volume += Math.abs(Number(trade.qty || 0));
    groups.set(key, group);
  }
  return Array.from(groups.values()).sort((a, b) => {
    if (a.key === 'unscheduled') return 1;
    if (b.key === 'unscheduled') return -1;
    return b.key.localeCompare(a.key);
  });
}

export function buildBulletinDays(days, planningEvents, dayReviews, newsCounts, activeKey) {
  const groups = new Map(days.map((day) => [day.key, { ...day }]));
  const addEmptyDay = (key) => {
    if (!groups.has(key)) groups.set(key, { key, label: formatDay(key), trades: [], pl: 0, volume: 0 });
  };
  Object.keys(planningEvents || {}).forEach(addEmptyDay);
  for (const key of Object.keys(dayReviews || {})) {
    if (hasReview(dayReviews[key])) addEmptyDay(key);
  }
  for (const key of newsCounts.keys()) addEmptyDay(key);
  if (activeKey) addEmptyDay(activeKey);
  return Array.from(groups.values()).sort((a, b) => {
    if (a.key === 'unscheduled') return 1;
    if (b.key === 'unscheduled') return -1;
    return b.key.localeCompare(a.key);
  });
}

export function summarizeMonth(days, monthDate) {
  const visibleDays = days
    .filter((day) => {
      if (day.key === 'unscheduled') return false;
      const [year, month] = day.key.split('-').map(Number);
      return year === monthDate.getFullYear() && month === monthDate.getMonth() + 1;
    })
    .sort((a, b) => a.key.localeCompare(b.key));
  const totalPl = visibleDays.reduce((sum, day) => sum + day.pl, 0);
  const tradeCount = visibleDays.reduce((sum, day) => sum + day.trades.length, 0);
  const winningDays = visibleDays.filter((day) => day.pl > 0).length;
  const losingDays = visibleDays.filter((day) => day.pl < 0).length;
  const bestDay = visibleDays.reduce((best, day) => (!best || day.pl > best.pl ? day : best), null);
  const worstDay = visibleDays.reduce((worst, day) => (!worst || day.pl < worst.pl ? day : worst), null);
  let cumulative = 0;
  let peak = 0;
  let maxDrawdown = 0;
  for (const day of visibleDays) {
    cumulative += day.pl;
    peak = Math.max(peak, cumulative);
    maxDrawdown = Math.min(maxDrawdown, cumulative - peak);
  }
  return { totalPl, tradeCount, winningDays, losingDays, bestDay, worstDay, maxDrawdown };
}

export function monthCurvePoints(days, monthDate) {
  const visibleDays = days
    .filter((day) => {
      if (day.key === 'unscheduled') return false;
      const [year, month] = day.key.split('-').map(Number);
      return year === monthDate.getFullYear() && month === monthDate.getMonth() + 1;
    })
    .sort((a, b) => a.key.localeCompare(b.key));
  let cumulative = 0;
  return visibleDays.map((day) => {
    cumulative += day.pl;
    return { key: day.key, label: day.label, value: cumulative };
  });
}

export function summarizeSymbolsForMonth(days, monthDate) {
  const rows = new Map();
  for (const day of days || []) {
    if (day.key === 'unscheduled') continue;
    const [year, month] = day.key.split('-').map(Number);
    if (year !== monthDate.getFullYear() || month !== monthDate.getMonth() + 1) continue;

    for (const trade of day.trades || []) {
      const symbol = String(trade?.symbol || 'UNKNOWN').toUpperCase();
      const row = rows.get(symbol) || {
        symbol,
        tradeCount: 0,
        wins: 0,
        losses: 0,
        pl: 0,
        volume: 0,
      };
      const pl = Number(trade?.pl || 0);
      row.tradeCount += 1;
      row.wins += pl > 0 ? 1 : 0;
      row.losses += pl < 0 ? 1 : 0;
      row.pl += pl;
      row.volume += Math.abs(Number(trade?.qty || 0));
      rows.set(symbol, row);
    }
  }
  return Array.from(rows.values()).sort((a, b) => Math.abs(b.pl) - Math.abs(a.pl));
}

export function buildReviewQueue(days, monthDate, dayReviews, autoEventsByDay, planningEvents) {
  return (days || [])
    .filter((day) => {
      if (day.key === 'unscheduled' || hasReview(dayReviews?.[day.key])) return false;
      const [year, month] = day.key.split('-').map(Number);
      return year === monthDate.getFullYear() && month === monthDate.getMonth() + 1;
    })
    .map((day) => {
      const autoEventCount = (autoEventsByDay.get(day.key) || []).length;
      const plannedEventCount = (planningEvents?.[day.key] || []).length;
      const reasons = [];
      if (day.pl < 0) reasons.push('Loss day');
      if (Math.abs(day.pl) >= 100) reasons.push('Large P/L');
      if (day.trades.length >= 5) reasons.push('High activity');
      if (autoEventCount + plannedEventCount > 0) reasons.push('Event day');
      return {
        ...day,
        autoEventCount,
        plannedEventCount,
        reasons,
      };
    })
    .filter((day) => day.reasons.length)
    .sort((a, b) => {
      const scoreA = Math.abs(a.pl) + a.trades.length * 10 + (a.autoEventCount + a.plannedEventCount) * 25;
      const scoreB = Math.abs(b.pl) + b.trades.length * 10 + (b.autoEventCount + b.plannedEventCount) * 25;
      return scoreB - scoreA;
    });
}

export function summarizeReviewsForMonth(days, monthDate, dayReviews, reviewQueue) {
  const monthKeys = new Set();
  for (const day of days || []) {
    if (day.key === 'unscheduled') continue;
    const [year, month] = day.key.split('-').map(Number);
    if (year === monthDate.getFullYear() && month === monthDate.getMonth() + 1) monthKeys.add(day.key);
  }
  for (const key of Object.keys(dayReviews || {})) {
    if (key === 'unscheduled') continue;
    const [year, month] = key.split('-').map(Number);
    if (year === monthDate.getFullYear() && month === monthDate.getMonth() + 1) monthKeys.add(key);
  }

  const tags = new Map();
  let reviewedDays = 0;
  let notedDays = 0;
  for (const key of monthKeys) {
    const review = dayReviews?.[key];
    if (!hasReview(review)) continue;
    reviewedDays += 1;
    if (review.notes?.trim()) notedDays += 1;
    for (const tag of review.tags || []) tags.set(tag, (tags.get(tag) || 0) + 1);
  }

  return {
    reviewedDays,
    notedDays,
    needsReviewDays: reviewQueue.length,
    tagCounts: Array.from(tags.entries())
      .map(([tag, count]) => ({ tag, count }))
      .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag)),
  };
}

export function buildMonthReviewExport({
  visibleMonth,
  days,
  monthSummary,
  symbolStats,
  reviewSummary,
  reviewQueue,
  dayReviews,
  planningEvents,
  autoEventsByDay,
}) {
  const monthDays = (days || [])
    .filter((day) => {
      if (day.key === 'unscheduled') return false;
      const [year, month] = day.key.split('-').map(Number);
      return year === visibleMonth.getFullYear() && month === visibleMonth.getMonth() + 1;
    })
    .sort((a, b) => a.key.localeCompare(b.key));

  return [
    `${monthLabel(visibleMonth)} Trading Review`,
    '',
    'Performance',
    `Total P/L: ${fmtMoney(monthSummary.totalPl)}`,
    `Trades: ${monthSummary.tradeCount}`,
    `Winning days: ${monthSummary.winningDays}`,
    `Losing days: ${monthSummary.losingDays}`,
    `Max drawdown: ${fmtMoney(monthSummary.maxDrawdown)}`,
    `Best day: ${monthSummary.bestDay ? `${monthSummary.bestDay.label} ${fmtMoney(monthSummary.bestDay.pl)}` : 'N/A'}`,
    `Worst day: ${monthSummary.worstDay ? `${monthSummary.worstDay.label} ${fmtMoney(monthSummary.worstDay.pl)}` : 'N/A'}`,
    '',
    'Review Coverage',
    `Reviewed days: ${reviewSummary.reviewedDays}`,
    `Days with notes: ${reviewSummary.notedDays}`,
    `Needs review: ${reviewSummary.needsReviewDays}`,
    ...(reviewSummary.tagCounts.length ? ['Tags:', ...reviewSummary.tagCounts.map((item) => `- ${item.tag}: ${item.count}`)] : ['Tags: N/A']),
    '',
    'Symbol Performance',
    ...(symbolStats.length
      ? symbolStats.map((row) => `- ${row.symbol}: ${fmtMoney(row.pl)} / ${row.tradeCount} trades / ${row.wins}W ${row.losses}L`)
      : ['No symbol performance for this month.']),
    '',
    'Needs Review',
    ...(reviewQueue.length
      ? reviewQueue.map((day) => `- ${day.label}: ${fmtMoney(day.pl)} / ${day.trades.length} trades / ${day.reasons.join(', ')}`)
      : ['No priority review days remaining.']),
    '',
    'Daily Detail',
    ...(monthDays.length
      ? monthDays.flatMap((day) => {
          const review = dayReviews?.[day.key] || emptyReview();
          const plans = planningEvents?.[day.key] || [];
          const events = autoEventsByDay.get(day.key) || [];
          return [
            '',
            `${day.label} (${day.key})`,
            `P/L: ${fmtMoney(day.pl)} / Trades: ${day.trades.length}`,
            ...day.trades.map((trade) => `- Trade ${trade.symbol || 'UNKNOWN'} qty ${trade.qty ?? 'N/A'} entry ${fmtMoney(trade.entry)} exit ${fmtMoney(trade.exit)} P/L ${fmtMoney(trade.pl)}`),
            ...(plans.length ? ['Planning:', ...plans.map((event) => `- ${event.type}: ${event.title}${event.note ? ` (${event.note})` : ''}`)] : []),
            ...(events.length ? ['Events:', ...events.map((event) => `- ${event.type}: ${event.title}${event.source ? ` / ${event.source}` : ''}`)] : []),
            ...(hasReview(review) ? ['Review:', ...(review.tags?.length ? [`Tags: ${review.tags.join(', ')}`] : []), ...(review.notes ? [`Notes: ${review.notes}`] : [])] : []),
          ];
        })
      : ['No closed trades for this month.']),
  ].join('\n');
}
