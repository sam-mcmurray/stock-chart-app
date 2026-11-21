export const autoEventFilters = ['All', 'IPO watch', 'Earnings watch', 'Economic release', 'Market note'];

const monthLookup = {
  jan: 0, january: 0, feb: 1, february: 1, mar: 2, march: 2, apr: 3, april: 3,
  may: 4, jun: 5, june: 5, jul: 6, july: 6, aug: 7, august: 7,
  sep: 8, sept: 8, september: 8, oct: 9, october: 9, nov: 10, november: 10,
  dec: 11, december: 11,
};

const weekdayLookup = {
  sunday: 0,
  monday: 1,
  tuesday: 2,
  wednesday: 3,
  thursday: 4,
  friday: 5,
  saturday: 6,
};

const padDatePart = (value) => String(value).padStart(2, '0');

function keyFromDateParts(year, month, day) {
  return `${year}-${padDatePart(month)}-${padDatePart(day)}`;
}

function dateToKey(date) {
  if (!date || Number.isNaN(date.getTime())) return 'unscheduled';
  return keyFromDateParts(date.getFullYear(), date.getMonth() + 1, date.getDate());
}

function timestampDateKey(timestamp) {
  if (!timestamp) return 'unscheduled';
  const date = new Date(Number(timestamp) * 1000);
  return dateToKey(date);
}

export function articleText(article) {
  return `${article?.title || ''} ${article?.summary || ''}`;
}

function publishedDate(article) {
  const date = new Date(Number(article?.published_ts || 0) * 1000);
  return Number.isNaN(date.getTime()) ? new Date() : date;
}

function inferYearForMonthDay(month, day, article) {
  const base = publishedDate(article);
  let candidate = new Date(base.getFullYear(), month, day);
  const daysBehind = (base.getTime() - candidate.getTime()) / (24 * 60 * 60 * 1000);
  if (daysBehind > 180) candidate = new Date(base.getFullYear() + 1, month, day);
  return candidate;
}

function inferWeekdayDate(weekday, article) {
  const base = publishedDate(article);
  const candidate = new Date(base.getFullYear(), base.getMonth(), base.getDate());
  const delta = (weekday - candidate.getDay() + 7) % 7 || 7;
  candidate.setDate(candidate.getDate() + delta);
  return candidate;
}

export function inferArticleEventKey(article) {
  if (/^20\d{2}-\d{2}-\d{2}$/.test(article?.calendar_date || '')) return article.calendar_date;
  const text = articleText(article);
  const explicitIso = text.match(/\b(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b/);
  if (explicitIso) return keyFromDateParts(Number(explicitIso[1]), Number(explicitIso[2]), Number(explicitIso[3]));
  const monthDay = text.match(/\b(?:on|for|due|scheduled|reports?|reporting|release(?:s|d)?|meeting|starts?|opens?|launch(?:es|ed)?)\s+(?:on\s+)?(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+([0-3]?\d)(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\b/i);
  if (monthDay) {
    const month = monthLookup[monthDay[1].toLowerCase().replace('.', '')];
    const day = Number(monthDay[2]);
    const year = monthDay[3] ? Number(monthDay[3]) : null;
    const date = year ? new Date(year, month, day) : inferYearForMonthDay(month, day, article);
    return dateToKey(date);
  }
  const weekday = text.match(/\b(?:on|due|scheduled|reports?|reporting|meeting|starts?|opens?|launch(?:es|ed)?)\s+(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b/i);
  if (weekday) return dateToKey(inferWeekdayDate(weekdayLookup[weekday[1].toLowerCase()], article));
  return timestampDateKey(article.published_ts);
}

export function persistentMarketNews(events) {
  return (Array.isArray(events) ? events : [])
    .filter((event) => (
      event?.details?.calendar_kind === 'market_news'
      && ['finance', 'business', 'markets'].includes(event.details?.relevance_label)
    ))
    .map((event) => ({
      id: event.id,
      title: event.details?.display_headline || event.title || 'Untitled market event',
      summary: event.details?.llm_summary || event.details?.summary || '',
      source: event.source || 'Unknown source',
      url: event.url,
      tickers: Array.from(new Set(
        Array.isArray(event.details?.related_tickers) ? event.details.related_tickers : [],
      )),
      published_ts: Number(event.details?.published_ts || 0),
      provider: event.details?.provider || 'unknown',
      providers: Array.isArray(event.details?.providers) ? event.details.providers : [],
      source_urls: Array.isArray(event.details?.source_urls) ? event.details.source_urls : [],
      calendar_date: event.date,
      persisted: true,
      relevance_label: event.details?.relevance_label || 'unclassified',
      relevance_score: Number(event.details?.relevance_score),
      relevance_confidence: Number(event.details?.relevance_confidence),
      relevance_reason: event.details?.relevance_reason || '',
      related_companies: Array.isArray(event.details?.related_companies) ? event.details.related_companies : [],
      related_commodities: Array.isArray(event.details?.related_commodities) ? event.details.related_commodities : [],
      llm_topics: Array.isArray(event.details?.llm_topics) ? event.details.llm_topics : [],
      company_relationships: Array.isArray(event.details?.company_relationships) ? event.details.company_relationships : [],
      headline_source: event.details?.headline_source || '',
      headline_verified: Boolean(event.details?.headline_verified),
      source_language: event.details?.source_language || 'English',
      translated_to_english: Boolean(event.details?.translated_to_english),
      news_topic: event.details?.news_topic || '',
    }));
}

function normalizedCalendarHeadline(value) {
  return String(value || '')
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function canonicalCalendarUrl(value) {
  try {
    const url = new URL(String(value || ''));
    url.hash = '';
    for (const key of [...url.searchParams.keys()]) {
      if (/^(utm_|source$|ref$|fbclid$|gclid$)/i.test(key)) url.searchParams.delete(key);
    }
    return `${url.hostname.toLowerCase()}${url.pathname.replace(/\/+$/, '')}${url.search}`;
  } catch {
    return String(value || '').trim().toLowerCase();
  }
}

function uniqueObjects(values, keyFn) {
  const unique = new Map();
  for (const value of values) {
    const key = keyFn(value);
    if (key && !unique.has(key)) unique.set(key, value);
  }
  return Array.from(unique.values());
}

function articleCoverage(article) {
  const coverage = Array.isArray(article.related_articles) ? article.related_articles : [];
  const candidates = [
    ...coverage,
    article.url ? {
      title: article.title,
      url: article.url,
      source: article.source,
      provider: article.provider,
    } : null,
    ...(article.source_urls || []).map((url) => ({
      title: article.title,
      url,
      source: url === article.url ? article.source : '',
      provider: url === article.url ? article.provider : '',
    })),
  ].filter((item) => item?.url);
  return uniqueObjects(candidates, (item) => canonicalCalendarUrl(item.url));
}

function mergeCalendarNews(left, right) {
  const leftSummary = left.summary || '';
  const rightSummary = right.summary || '';
  return {
    ...left,
    title: left.title || right.title,
    summary: rightSummary.length > leftSummary.length ? rightSummary : leftSummary,
    tickers: Array.from(new Set([...(left.tickers || []), ...(right.tickers || [])])).sort(),
    providers: Array.from(new Set([
      left.provider, right.provider, left.source, right.source,
      ...(left.providers || []), ...(right.providers || []),
    ].filter(Boolean))),
    source_urls: Array.from(new Set([
      left.url, right.url, ...(left.source_urls || []), ...(right.source_urls || []),
    ].filter(Boolean))),
    related_articles: uniqueObjects(
      [...articleCoverage(left), ...articleCoverage(right)],
      (article) => canonicalCalendarUrl(article.url),
    ),
    related_companies: uniqueObjects(
      [...(left.related_companies || []), ...(right.related_companies || [])],
      (company) => String(company?.name || '').toLowerCase(),
    ),
    related_commodities: Array.from(new Set([
      ...(left.related_commodities || []), ...(right.related_commodities || []),
    ])),
    llm_topics: Array.from(new Set([...(left.llm_topics || []), ...(right.llm_topics || [])])),
    company_relationships: uniqueObjects(
      [...(left.company_relationships || []), ...(right.company_relationships || [])],
      (relationship) => [
        relationship?.company_a,
        relationship?.relationship,
        relationship?.company_b,
      ].map((value) => String(value || '').toLowerCase()).join('|'),
    ),
    relevance_confidence: Math.max(
      Number(left.relevance_confidence || 0),
      Number(right.relevance_confidence || 0),
    ),
  };
}

export function deduplicateCalendarNews(items) {
  const groups = [];
  const indexes = new Map();
  for (const article of Array.isArray(items) ? items : []) {
    const day = article.calendar_date || inferArticleEventKey(article);
    const titleKey = normalizedCalendarHeadline(article.title);
    const urlKey = canonicalCalendarUrl(article.url);
    const keys = [
      urlKey && `url:${urlKey}`,
      titleKey && day && `title:${day}:${titleKey}`,
    ].filter(Boolean);
    const existingIndex = keys.map((key) => indexes.get(key)).find(Number.isInteger);
    if (existingIndex === undefined) {
      const index = groups.length;
      groups.push({
        ...article,
        source_urls: Array.from(new Set([
          article.url,
          ...(article.source_urls || []),
        ].filter(Boolean))),
        related_articles: articleCoverage(article),
      });
      keys.forEach((key) => indexes.set(key, index));
      continue;
    }
    groups[existingIndex] = mergeCalendarNews(groups[existingIndex], article);
    keys.forEach((key) => indexes.set(key, existingIndex));
  }
  return groups;
}

export function buildSymbolUniverse(summary) {
  const symbols = new Set();
  for (const trade of Array.isArray(summary?.recent_trades) ? summary.recent_trades : []) {
    if (trade?.symbol) symbols.add(String(trade.symbol).toUpperCase());
  }
  for (const position of Array.isArray(summary?.positions) ? summary.positions : []) {
    if (position?.symbol) symbols.add(String(position.symbol).toUpperCase());
  }
  return Array.from(symbols).sort();
}

export const marketEventDefinitions = [
  {
    title: 'IPOs',
    topics: ['ipo', 'initial public offering', 'public offering', 'direct listing', 'spac'],
  },
  {
    title: 'Earnings News',
    topics: ['earnings', 'eps', 'guidance', 'quarterly results'],
  },
  {
    title: 'Economic Releases',
    topics: ['cpi', 'ppi', 'jobs report', 'nonfarm payrolls', 'inflation', 'interest rates', 'gdp', 'pce', 'fomc'],
  },
  {
    title: 'Market Notes',
    topics: null,
  },
];

export function buildMarketEventSections(newsItems) {
  const recent = Array.isArray(newsItems) ? newsItems : [];
  return marketEventDefinitions.map((definition) => {
    const matching = definition.topics
      ? recent.filter((article) => {
        const topics = new Set([
          ...(article.llm_topics || []),
          article.news_topic,
        ].filter(Boolean).map((topic) => String(topic).toLowerCase()));
        return definition.topics.some((topic) => topics.has(topic));
      })
      : recent;
    return {
      ...definition,
      articles: definition.topics ? matching.slice(0, 8) : matching,
      totalCount: matching.length,
    };
  });
}

export function classifyAutoEvent(article) {
  const text = articleText(article);
  if (/\b(ipo|initial public offering|public debut|direct listing|spac)\b/i.test(text)) return 'IPO watch';
  if (/\b(earnings|eps|revenue|guidance|quarterly results|profit forecast)\b/i.test(text)) return 'Earnings watch';
  if (/\b(cpi|ppi|jobs report|nonfarm|payrolls|fed|fomc|inflation|rates|gdp|pce)\b/i.test(text)) return 'Economic release';
  return 'Market note';
}

function symbolsInArticle(article, symbols) {
  const text = articleText(article).toUpperCase();
  return symbols.filter((symbol) => new RegExp(`(^|[^A-Z])${symbol}([^A-Z]|$)`).test(text));
}

export function buildAutoEventsByDay(newsItems, symbols) {
  const groups = new Map();
  for (const article of Array.isArray(newsItems) ? newsItems : []) {
    const key = inferArticleEventKey(article);
    if (key === 'unscheduled') continue;
    const group = groups.get(key) || [];
    group.push({
      id: article.url || `${key}-${article.title || group.length}`,
      type: classifyAutoEvent(article),
      title: article.title || 'Untitled market event',
      source: article.source || 'Unknown source',
      url: article.url,
      symbols: symbolsInArticle(article, symbols),
    });
    groups.set(key, group);
  }
  return groups;
}

export function buildExtractedEventsByDay(events) {
  const groups = new Map();
  for (const event of Array.isArray(events) ? events : []) {
    const key = event?.date;
    if (!key || key === 'unscheduled') continue;
    const group = groups.get(key) || [];
    group.push({
      id: event.id || event.url || `${key}-${event.title || group.length}`,
      type: autoEventFilters.includes(event.type) && event.type !== 'All' ? event.type : 'Market note',
      title: event.title || 'Untitled market event',
      source: event.source || 'Unknown source',
      url: event.url,
      symbols: Array.from(new Set([
        ...(Array.isArray(event.symbols) ? event.symbols : []),
        ...(Array.isArray(event.details?.related_tickers) ? event.details.related_tickers : []),
      ].map((symbol) => String(symbol).toUpperCase()))),
      confidence: event.confidence,
      extractionSource: event.extraction_source,
      details: event.details && typeof event.details === 'object' ? event.details : {},
    });
    groups.set(key, group);
  }
  return groups;
}

export function countAutoEvents(events) {
  const counts = new Map();
  for (const event of events || []) counts.set(event.type, (counts.get(event.type) || 0) + 1);
  return counts;
}

export function formatAutoEventBadges(events) {
  const counts = countAutoEvents(events);
  return [
    ['IPO watch', 'IPO'],
    ['Earnings watch', 'Earn'],
    ['Economic release', 'Econ'],
    ['Market note', 'Mkt'],
  ].map(([type, label]) => ({ type, label, count: counts.get(type) || 0 })).filter((item) => item.count > 0);
}

export function confidenceTone(confidence) {
  if (!Number.isFinite(confidence)) return '';
  if (confidence >= 0.75) return 'high';
  if (confidence >= 0.55) return 'medium';
  return 'low';
}

export function daySearchText(day, { dayReviews, planningEvents, autoEventsByDay, news }) {
  const autoEvents = autoEventsByDay.get(day.key) || [];
  const dayNews = (Array.isArray(news) ? news : []).filter((article) => inferArticleEventKey(article) === day.key);
  return [
    day.label,
    day.key,
    ...day.trades.map((trade) => `${trade.symbol || ''} ${trade.bucket || ''}`),
    dayReviews[day.key]?.notes || '',
    ...(dayReviews[day.key]?.tags || []),
    ...(planningEvents[day.key] || []).flatMap((event) => [event.type, event.title, event.note]),
    ...autoEvents.flatMap((event) => [event.type, event.title, event.source, ...(event.symbols || [])]),
    ...dayNews.flatMap((article) => [article.title, article.source, article.summary]),
  ].join(' ').toLowerCase();
}
