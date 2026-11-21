import html
import re
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.iposcoop.com"
CALENDAR_URL = f"{BASE_URL}/ipo-calendar/"
BUZZ_API_URL = f"{BASE_URL}/wp-json/wp/v2/posts"
HEADERS = {"User-Agent": "stock-chart-app/1.0 calendar importer"}


def _get(url: str, *, params: dict | None = None, timeout: float = 20):
    response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def _date(value: str) -> str | None:
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", value or "")
    if not match:
        return None
    month, day, year = map(int, match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _table_rows(page_html: str) -> tuple[list[str], list[dict[str, Any]]]:
    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.select_one("table.ipolist")
    if table is None:
        return [], []
    headers = [cell.get_text(" ", strip=True).lower() for cell in table.select("thead th")]
    rows = []
    for row in table.select("tbody tr"):
        cells = row.select("td")
        values = [cell.get_text(" ", strip=True) for cell in cells]
        if not values:
            continue
        link = cells[0].find("a", href=True)
        rows.append({"values": values, "url": link["href"] if link else None})
    return headers, rows


def load_ipo_calendar(timeout: float = 20) -> list[dict[str, Any]]:
    headers, rows = _table_rows(_get(CALENDAR_URL, timeout=timeout).text)
    expected_index = headers.index("expected to trade")
    events = []
    for row in rows:
        values = row["values"]
        event_date = _date(values[expected_index])
        if not event_date:
            continue
        company = html.unescape(values[0]).strip()
        symbol = values[1].strip().upper() if len(values) > 1 else ""
        stable_key = re.sub(r"[^a-z0-9]+", "-", (row["url"] or symbol or company).lower()).strip("-")
        events.append({
            "id": f"iposcoop:calendar:{stable_key}",
            "date": event_date,
            "type": "IPO watch",
            "title": company,
            "source": "IPO Scoop Calendar",
            "url": row["url"] or CALENDAR_URL,
            "symbols": [symbol] if symbol else [],
            "confidence": 0.95,
            "extraction_source": "ipo_scoop_calendar",
            "details": {
                "date_basis": "expected_trade_date",
                "date_label": values[expected_index],
                "status": "priced" if "priced" in values[expected_index].lower() else "expected",
            },
        })
    return events


def load_ipo_pricings(year: int, timeout: float = 20) -> list[dict[str, Any]]:
    url = f"{BASE_URL}/current-year-pricings/" if year == datetime.now().year else f"{BASE_URL}/{year}-pricings/"
    headers, rows = _table_rows(_get(url, timeout=timeout).text)
    if "offer date" not in headers:
        return []
    date_index = headers.index("offer date")
    events = []
    for row in rows:
        values = row["values"]
        event_date = _date(values[date_index])
        if not event_date:
            continue
        company = html.unescape(values[0]).strip()
        symbol = values[1].strip().upper() if len(values) > 1 else ""
        events.append({
            "id": f"iposcoop:pricing:{symbol or company.lower()}:{event_date}",
            "date": event_date,
            "type": "IPO watch",
            "title": company,
            "source": "IPO Scoop Pricings",
            "url": row["url"] or url,
            "symbols": [symbol] if symbol else [],
            "confidence": 1.0,
            "extraction_source": "ipo_scoop_pricing",
            "details": {"date_basis": "offer_date", "status": "priced"},
        })
    return events


def load_ipo_buzz_page(page: int = 1, per_page: int = 100, order: str = "asc", timeout: float = 20):
    response = _get(BUZZ_API_URL, params={
        "categories": 3,
        "page": page,
        "per_page": per_page,
        "orderby": "date",
        "order": order,
        "_fields": "id,date,modified,link,title,excerpt",
    }, timeout=timeout)
    events = []
    for post in response.json():
        title = BeautifulSoup(html.unescape(post.get("title", {}).get("rendered", "")), "html.parser").get_text(" ", strip=True)
        published = str(post.get("date") or "")[:10]
        if not title or not re.match(r"^20\d{2}-\d{2}-\d{2}$", published):
            continue
        proposed = re.findall(r"\(([A-Z][A-Z0-9.]{0,7})\s+Proposed\)", title, re.I)
        events.append({
            "id": f"iposcoop:buzz:{post['id']}",
            "date": published,
            "type": "IPO watch",
            "title": title,
            "source": "IPO Scoop Buzz",
            "url": post.get("link"),
            "symbols": sorted({symbol.upper() for symbol in proposed}),
            "confidence": 1.0,
            "extraction_source": "ipo_scoop_buzz",
            "details": {
                "date_basis": "article_publication_date",
                "status": "editorial",
                "post_id": post["id"],
            },
        })
    return events, int(response.headers.get("X-WP-TotalPages") or 1)
