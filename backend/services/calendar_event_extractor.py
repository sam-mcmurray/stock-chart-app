import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


CALENDAR_TIMEZONE = ZoneInfo("America/New_York")
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _text(article: dict) -> str:
    return f"{article.get('title') or ''} {article.get('summary') or article.get('description') or ''}"


def _published(article: dict) -> datetime:
    try:
        return datetime.fromtimestamp(int(article.get("published_ts") or time.time()), tz=CALENDAR_TIMEZONE)
    except (TypeError, ValueError, OSError):
        return datetime.now(CALENDAR_TIMEZONE)


def classify_news_event(article: dict) -> str:
    text = _text(article).lower()
    # IPO dates are authoritative only when supplied by IPO Scoop.
    if re.search(r"\b(cpi|ppi|jobs report|nonfarm|payrolls|fed|fomc|inflation|rates|gdp|pce)\b", text):
        return "Economic release"
    return "Market note"


def infer_news_event_date(article: dict) -> str:
    text = _text(article)
    base = _published(article)
    iso = re.search(r"\b(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b", text)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    month_day = re.search(
        r"\b(?:on|for|due|scheduled|reports?|reporting|release(?:s|d)?|meeting|starts?|opens?|launch(?:es|ed)?)\s+(?:on\s+)?"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
        r"([0-3]?\d)(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\b",
        text, re.I,
    )
    if month_day:
        month = MONTHS[month_day.group(1).lower().replace(".", "")]
        day = int(month_day.group(2))
        year = int(month_day.group(3)) if month_day.group(3) else base.year
        candidate = datetime(year, month, day, tzinfo=CALENDAR_TIMEZONE)
        if not month_day.group(3) and (base - candidate).days > 180:
            candidate = candidate.replace(year=year + 1)
        return candidate.date().isoformat()
    weekday = re.search(
        r"\b(?:on|due|scheduled|reports?|reporting|meeting|starts?|opens?|launch(?:es|ed)?)\s+"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text, re.I,
    )
    if weekday:
        delta = (WEEKDAYS[weekday.group(1).lower()] - base.weekday()) % 7 or 7
        return (base + timedelta(days=delta)).date().isoformat()
    return base.date().isoformat()


def news_article_calendar_event(article: dict) -> dict:
    url = article.get("canonical_url") or article.get("url") or ""
    event_id = article.get("dedup_id") or article.get("id") or url
    published_ts = int(article.get("published_ts") or time.time())
    published_date = _published({"published_ts": published_ts}).date().isoformat()
    inferred_event_date = infer_news_event_date({
        **article,
        "title": article.get("original_title") or article.get("title"),
    })
    earnings_news = bool(re.search(
        r"\b(earnings|eps|revenue|guidance|quarterly results|profit forecast)\b",
        _text(article),
        re.I,
    ))
    return {
        "id": str(event_id),
        "date": published_date,
        "type": classify_news_event(article),
        "title": article.get("display_headline") or article.get("title") or "Untitled market event",
        "source": article.get("source") or "Unknown source",
        "url": url or None,
        "symbols": article.get("tickers") or [],
        "confidence": 0.55,
        "extraction_source": "news_rules",
        "details": {
            "calendar_kind": "market_news",
            "summary": article.get("summary") or "",
            "llm_summary": article.get("llm_summary") or "",
            "published_ts": published_ts,
            "provider": article.get("provider") or "unknown",
            "providers": article.get("providers") or [article.get("provider") or "unknown"],
            "source_urls": article.get("source_urls") or ([url] if url else []),
            "event_ids": article.get("event_ids") or ([str(article["event_id"])] if article.get("event_id") else []),
            "title_inferred": bool(article.get("title_inferred")),
            "date_basis": "article_publication_date",
            "inferred_event_date": inferred_event_date if inferred_event_date != published_date else None,
            "news_topic": "earnings" if earnings_news else None,
            "relevance_label": article.get("relevance_label"),
            "relevance_score": article.get("relevance_score"),
            "relevance_confidence": article.get("relevance_confidence"),
            "relevance_reason": article.get("relevance_reason") or "",
            "relevance_source": article.get("relevance_source"),
            "relevance_model": article.get("relevance_model"),
            "display_headline": article.get("display_headline"),
            "headline_source": article.get("headline_source"),
            "headline_verified": article.get("headline_verified"),
            "source_language": article.get("source_language"),
            "translated_to_english": bool(article.get("translated_to_english")),
            "llm_topics": article.get("llm_topics") or [],
            "related_commodities": article.get("related_commodities") or [],
            "related_companies": article.get("related_companies") or [],
            "company_relationships": article.get("company_relationships") or [],
            "related_tickers": article.get("tickers") or [],
        },
    }
