import logging
import os
import re
import time

from neo4j import GraphDatabase
from services.ticker_utils import is_plausible_ticker

logger = logging.getLogger(__name__)

_CACHE = {"loaded_at": 0.0, "rows": [], "index": {}, "companies": {}}
_CACHE_SECONDS = int(os.getenv("SECURITY_MASTER_CACHE_SECONDS", "3600"))
_GENERIC_ALIAS_TOKENS = {
    "american", "bank", "capital", "company", "corp", "corporation", "financial",
    "first", "global", "group", "holding", "holdings", "international", "national",
    "resources", "technologies", "technology", "the", "united",
}
_NON_PRIMARY_EXCHANGES = {"", "UNKNOWN", "OTC"}
_CORPORATE_SUFFIX_TOKENS = {
    "a", "ag", "and", "as", "co", "company", "corp", "corporation", "group",
    "holding", "holdings", "inc", "limited", "ltd", "n", "nv", "plc", "s",
    "sa", "se",
}


def normalize_company_text(value: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip() + " "


def select_alias_rows(records) -> list[tuple[str, str]]:
    normalized = []
    for record in records:
        alias = str(record.get("alias") or "").strip().lower()
        symbol = str(record.get("symbol") or "").strip().upper()
        company_key = str(record.get("company_key") or "")
        exchange = str(record.get("exchange") or "UNKNOWN").strip().upper()
        if not alias or not is_plausible_ticker(symbol):
            continue
        if set(alias.split()).issubset(_GENERIC_ALIAS_TOKENS):
            continue
        normalized.append((alias, symbol, company_key, exchange))

    alias_companies = {}
    for alias, _symbol, company_key, _exchange in normalized:
        alias_companies.setdefault(alias, set()).add(company_key)

    grouped = {}
    for alias, symbol, company_key, exchange in normalized:
        if len(alias_companies[alias]) != 1:
            continue
        grouped.setdefault((alias, company_key), []).append((symbol, exchange))

    selected = []
    for (alias, _company_key), securities in grouped.items():
        primary = [row for row in securities if row[1] not in _NON_PRIMARY_EXCHANGES]
        chosen = primary
        if not chosen and len(securities) == 1:
            chosen = securities
        elif not chosen and len(securities) > 1:
            ordered = sorted(securities, key=lambda row: (len(row[0]), row[0]))
            if len(ordered[0][0]) < len(ordered[1][0]):
                chosen = [ordered[0]]
        for symbol, _exchange in chosen:
            selected.append((alias, symbol))
    return sorted(set(selected))


def _load_alias_rows() -> list[tuple[str, str]]:
    now = time.time()
    if _CACHE["rows"] and now - _CACHE["loaded_at"] < _CACHE_SECONDS:
        return _CACHE["rows"]
    url = os.getenv("NEO4J_URL", "bolt://neo4j:7687")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    auth = (user, password) if user and password else None
    try:
        with GraphDatabase.driver(url, auth=auth) as driver:
            records = driver.execute_query(
                """
                MATCH (c:Company)-[:ISSUES]->(s:Security)-[:LISTED_ON]->(e:Exchange)
                WHERE coalesce(s.is_primary_equity, true) AND NOT coalesce(s.is_etf, false)
                UNWIND coalesce(c.aliases, []) AS alias
                RETURN c.key AS company_key, c.name AS company_name,
                       s.name AS security_name, alias, s.symbol AS symbol,
                       e.code AS exchange
                """,
            ).records
            raw_records = [dict(record) for record in records]
            rows = select_alias_rows(raw_records)
        _CACHE.update({
            "loaded_at": now,
            "rows": rows,
            "companies": {
                str(record.get("symbol") or "").upper(): {
                    "name": str(record.get("company_name") or record.get("security_name") or "").strip(),
                    "exchange": str(record.get("exchange") or "").strip(),
                    "source": "security_master",
                }
                for record in raw_records
                if record.get("symbol") and (record.get("company_name") or record.get("security_name"))
            },
        })
        index = {}
        for alias, symbol in _CACHE["rows"]:
            first = alias.split(" ", 1)[0]
            index.setdefault(first, []).append((alias, symbol))
        _CACHE["index"] = index
    except Exception as exc:
        logger.info("Security master aliases unavailable: %s", exc)
        return []
    return _CACHE["rows"]


def resolve_company_tickers(
    text: str,
    limit: int = 20,
    *,
    exact_company_name: bool = False,
) -> list[str]:
    original = str(text or "")
    normalized = normalize_company_text(text)
    _load_alias_rows()
    matches = []
    candidates = set()
    input_tokens = normalized.split()
    input_core = _company_core(input_tokens) if 2 <= len(input_tokens) <= 8 else []
    for token in set(normalized.split()):
        candidates.update(_CACHE.get("index", {}).get(token, []))
    for alias, symbol in sorted(candidates):
        needle = f" {alias} "
        corporate_name_match = bool(
            input_core
            and (len(input_core) >= 2 or (
                exact_company_name
                and len(input_tokens) > len(input_core)
                and len(alias.split()) > len(input_core)
            ))
            and _company_core(alias.split()) == input_core
        )
        if exact_company_name and not corporate_name_match:
            continue
        if needle in normalized or corporate_name_match:
            if " " not in alias:
                titled = alias[:1].upper() + alias[1:]
                if not re.search(rf"\b(?:{re.escape(titled)}|{re.escape(alias.upper())})\b", original):
                    continue
                position = normalized.find(needle)
                context = normalized[max(0, position - 80):position + len(needle) + 80]
                if not re.search(
                    r"\b(stock|stocks|share|shares|earnings|revenue|company|investor|"
                    r"valuation|dividend|buy|sell|nasdaq|nyse|ticker)\b",
                    context,
                ):
                    continue
            matches.append(symbol)
            if len(matches) >= limit:
                break
    return sorted(set(matches))


def _company_core(tokens: list[str]) -> list[str]:
    core = list(tokens)
    while len(core) > 1 and core[-1] in _CORPORATE_SUFFIX_TOKENS:
        core.pop()
    return core


def validate_security_symbols(symbols) -> list[str]:
    rows = _load_alias_rows()
    known = {symbol for _alias, symbol in rows}
    return sorted({
        str(symbol or "").strip().upper().lstrip("$")
        for symbol in symbols or []
        if str(symbol or "").strip().upper().lstrip("$") in known
    })


def security_company_identity(symbol: str) -> dict:
    _load_alias_rows()
    return dict(_CACHE.get("companies", {}).get(str(symbol or "").upper()) or {})


def clear_security_master_cache():
    _CACHE.update({"loaded_at": 0.0, "rows": [], "index": {}, "companies": {}})
