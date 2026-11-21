import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


class GDELTRateLimitError(RuntimeError):
    pass


def _parse_seen_date(value: str | None) -> int:
    if not value:
        return 0
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


def _article_payload(article: dict) -> dict:
    return {
        "source": article.get("domain") or article.get("sourceCountry") or "GDELT",
        "headline": article.get("title") or "",
        "summary": article.get("snippet") or "",
        "url": article.get("url"),
        "datetime": _parse_seen_date(article.get("seendate")),
        "id": hash(article.get("url")),
        "related": [],
        "image": article.get("socialimage") or "",
    }


def load_gdelt_articles(
    query: str,
    timespan: str = "1h",
    max_records: int = 75,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
    sort: str = "datedesc",
) -> list[dict]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "sort": sort,
    }
    if start_datetime and end_datetime:
        params["startdatetime"] = start_datetime
        params["enddatetime"] = end_datetime
    else:
        params["timespan"] = timespan

    try:
        response = requests.get(GDELT_DOC_URL, params=params, timeout=30)
        if response.status_code == 429:
            raise GDELTRateLimitError("GDELT returned HTTP 429")
        response.raise_for_status()
        data = response.json()
        return [
            _article_payload(article)
            for article in data.get("articles", [])
            if article.get("url") and article.get("title")
        ]
    except GDELTRateLimitError:
        raise
    except Exception as exc:
        logger.warning("GDELT fetch failed: %s", exc)
        return []
