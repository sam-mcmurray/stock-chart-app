import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests


BASE_URL = "https://finance.yahoo.com/calendar/earnings"
PAGE_SIZE = 100
CALL_TIME_LABELS = {
    "BMO": "before_market_open",
    "AMC": "after_market_close",
    "DMH": "during_market_hours",
    "TAS": "time_not_supplied",
}


class YahooEarningsError(RuntimeError):
    pass


class YahooEarningsRateLimited(YahooEarningsError):
    pass


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=(day.weekday() + 1) % 7)
    return start, start + timedelta(days=6)


def earnings_url(day: date | str, *, offset: int = 0, size: int = PAGE_SIZE) -> str:
    selected = _as_date(day)
    start, end = _week_bounds(selected)
    return f"{BASE_URL}?{urlencode({
        'from': start.isoformat(),
        'to': end.isoformat(),
        'day': selected.isoformat(),
        'offset': max(0, int(offset)),
        'size': max(1, min(int(size), PAGE_SIZE)),
    })}"


def _number(value: str) -> float | None:
    text = str(value or "").strip().replace(",", "").replace("%", "")
    if text in {"", "-", "N/A", "n/a"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        parsed = float(text)
    except ValueError:
        return None
    return -parsed if negative else parsed


def _market_cap(value: str) -> int | None:
    text = str(value or "").strip().replace(",", "")
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([KMBT])?", text, re.I)
    if not match:
        return None
    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
        "T": 1_000_000_000_000,
    }[str(match.group(2) or "").upper()]
    return int(float(match.group(1)) * multiplier)


def _selected_date(soup: BeautifulSoup, requested: date) -> date | None:
    info = soup.select_one('[data-testid="cal-info"]')
    text = info.get_text(" ", strip=True) if info else ""
    match = re.search(r"Earnings On \w+,\s+([A-Za-z]{3})\s+(\d{1,2})", text)
    if not match:
        return None
    return datetime.strptime(
        f"{requested.year} {match.group(1)} {match.group(2)}",
        "%Y %b %d",
    ).date()


def _is_empty_requested_week(soup: BeautifulSoup, requested: date) -> bool:
    if soup.select_one("table") is not None:
        return False
    heading = next(
        (node for node in soup.select("h1,h2,h3") if "Earnings Calendar for:" in node.get_text(" ", strip=True)),
        None,
    )
    text = heading.parent.get_text(" ", strip=True) if heading and heading.parent else ""
    match = re.search(
        r"([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})\s*-\s*"
        r"([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})",
        text,
    )
    if not match:
        return False
    actual_start = datetime.strptime(" ".join(match.groups()[:3]), "%b %d %Y").date()
    actual_end = datetime.strptime(" ".join(match.groups()[3:]), "%b %d %Y").date()
    expected_start, expected_end = _week_bounds(requested)
    return (actual_start, actual_end) == (expected_start, expected_end)


def parse_earnings_page(page_html: str, requested_day: date | str, source_url: str) -> tuple[list[dict[str, Any]], int]:
    requested = _as_date(requested_day)
    soup = BeautifulSoup(page_html, "html.parser")
    selected = _selected_date(soup, requested)
    if selected != requested:
        if _is_empty_requested_week(soup, requested):
            return [], 0
        raise YahooEarningsError(
            f"Yahoo returned {selected.isoformat() if selected else 'an unknown day'} "
            f"for requested day {requested.isoformat()}"
        )
    table = soup.select_one("table")
    if table is None:
        return [], 0

    headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
    normalized = {header.strip().lower(): index for index, header in enumerate(headers)}
    required = {
        "symbol", "company", "event name", "earnings call time",
        "eps estimate", "reported eps", "surprise (%)", "market cap",
    }
    missing = required.difference(normalized)
    if missing:
        raise YahooEarningsError(f"Yahoo earnings columns changed: missing {sorted(missing)}")

    total_match = re.search(r"\d+\s*-\s*\d+\s+of\s+([\d,]+)", soup.get_text(" ", strip=True))
    total = int(total_match.group(1).replace(",", "")) if total_match else 0
    events = []
    for row in table.select("tbody tr"):
        values = [cell.get_text(" ", strip=True) for cell in row.select("td")]
        if len(values) < len(headers):
            continue
        raw = {header: values[index] for index, header in enumerate(headers)}
        symbol = values[normalized["symbol"]].strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", symbol):
            continue
        company = values[normalized["company"]].strip()
        event_name = values[normalized["event name"]].strip()
        if event_name == "-":
            event_name = ""
        call_time = values[normalized["earnings call time"]].strip().upper()
        quarter_match = re.search(r"\bQ([1-4])\s+(\d{4})\b", event_name, re.I)
        identity = hashlib.sha256(
            f"yahoo|{symbol}|{requested.isoformat()}".encode("utf-8")
        ).hexdigest()
        market_cap_text = values[normalized["market cap"]].strip()
        events.append({
            "id": f"yahoo:{identity}",
            "source": "yahoo",
            "symbol": symbol,
            "company_name": company or None,
            "event_date": requested.isoformat(),
            "event_name": event_name or None,
            "call_time": call_time or None,
            "fiscal_quarter": int(quarter_match.group(1)) if quarter_match else None,
            "fiscal_year": int(quarter_match.group(2)) if quarter_match else None,
            "eps_estimate": _number(values[normalized["eps estimate"]]),
            "eps_actual": _number(values[normalized["reported eps"]]),
            "surprise_percent": _number(values[normalized["surprise (%)"]]),
            "market_cap_text": market_cap_text or None,
            "market_cap_value": _market_cap(market_cap_text),
            "source_url": source_url,
            "raw_payload": raw,
        })
    return events, max(total, len(events))


def load_earnings_page(
    day: date | str,
    *,
    offset: int = 0,
    size: int = PAGE_SIZE,
    timeout: float = 30,
    get=None,
) -> tuple[list[dict[str, Any]], int]:
    url = earnings_url(day, offset=offset, size=size)
    fetch = get or curl_requests.get
    response = fetch(url, impersonate="chrome", timeout=timeout)
    if response.status_code == 429:
        raise YahooEarningsRateLimited("Yahoo earnings calendar returned HTTP 429")
    if response.status_code != 200:
        raise YahooEarningsError(f"Yahoo earnings calendar returned HTTP {response.status_code}")
    return parse_earnings_page(response.text, day, url)


def calendar_event(event: dict[str, Any]) -> dict[str, Any]:
    symbol = event["symbol"]
    event_name = event.get("event_name") or "Earnings Announcement"
    call_time = event.get("call_time") or ""
    return {
        "id": f"earnings:{event['id']}",
        "date": event["event_date"],
        "type": "Earnings watch",
        "title": f"{symbol} {event_name}",
        "source": "Yahoo Finance Earnings Calendar",
        "url": event.get("source_url"),
        "symbols": [symbol],
        "confidence": 0.80,
        "extraction_source": "yahoo_earnings",
        "details": {
            "date_basis": "earnings_calendar_date",
            "source_tier": "calendar_provider",
            "company_name": event.get("company_name"),
            "call_time": call_time or None,
            "call_time_label": CALL_TIME_LABELS.get(call_time, "unknown"),
            "fiscal_quarter": event.get("fiscal_quarter"),
            "fiscal_year": event.get("fiscal_year"),
            "eps_estimate": event.get("eps_estimate"),
            "eps_actual": event.get("eps_actual"),
            "surprise_percent": event.get("surprise_percent"),
            "market_cap_text": event.get("market_cap_text"),
            "market_cap_value": event.get("market_cap_value"),
            "status": "reported" if event.get("eps_actual") is not None else "scheduled",
            "earnings_event_id": event["id"],
        },
    }
