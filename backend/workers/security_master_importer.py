import csv
import io
import json
import logging
import os
import re

import requests
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_GENERIC_ALIASES = {
    "company", "corporation", "group", "holdings", "international", "global",
    "capital", "financial", "technologies", "technology", "energy", "resources",
    "bank", "first", "national", "united", "american",
}
_SUFFIXES = re.compile(
    r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|group|class [a-z])\b",
    re.IGNORECASE,
)


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def company_aliases(name: str, symbol: str = "") -> list[str]:
    full = _normalized(name)
    shortened = _normalized(_SUFFIXES.sub(" ", name))
    dotcom_brand = _normalized(_SUFFIXES.sub(" ", re.sub(r"\.com\b", " ", name, flags=re.IGNORECASE)))
    aliases = {full, shortened, dotcom_brand}
    first_word = shortened.split(" ", 1)[0] if shortened else ""
    if first_word and first_word == str(symbol or "").lower():
        aliases.add(first_word)
    parts = shortened.split()
    if len(parts) >= 2 and parts[1] == "com":
        aliases.add(parts[0])
    aliases = {
        alias for alias in aliases
        if len(alias) >= 4 and alias not in _GENERIC_ALIASES and not alias.isdigit()
    }
    return sorted(aliases)


def parse_sec_companies(payload: dict) -> list[dict]:
    fields = payload.get("fields") or []
    rows = []
    for values in payload.get("data") or []:
        item = dict(zip(fields, values))
        symbol = str(item.get("ticker") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not symbol or not name:
            continue
        cik = str(item.get("cik") or "").strip()
        rows.append({
            "symbol": symbol,
            "name": name,
            "exchange": str(item.get("exchange") or "UNKNOWN").strip().upper(),
            "cik": cik,
            "company_key": f"cik:{cik}" if cik else f"name:{_normalized(name)}",
            "aliases": company_aliases(name, symbol),
            "listing_name": name,
            "is_primary_equity": True,
            "is_etf": False,
            "source": "sec",
        })
    return rows


def parse_symbol_directory(text: str, *, nasdaq: bool) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    rows = []
    for item in reader:
        symbol = str(item.get("Symbol") if nasdaq else item.get("ACT Symbol") or "").strip().upper()
        name = str(item.get("Security Name") or "").strip()
        if not symbol or not name or symbol.startswith("File Creation Time"):
            continue
        if str(item.get("Test Issue") or "N").upper() == "Y":
            continue
        exchange = "NASDAQ" if nasdaq else {
            "A": "NYSE AMERICAN", "N": "NYSE", "P": "NYSE ARCA", "V": "IEX", "Z": "CBOE BZX",
        }.get(str(item.get("Exchange") or "").upper(), "OTHER")
        key_name = _normalized(name)
        rows.append({
            "symbol": symbol,
            "name": name,
            "exchange": exchange,
            "cik": "",
            "company_key": f"name:{key_name}",
            "aliases": company_aliases(name, symbol),
            "listing_name": name,
            "is_primary_equity": not bool(re.search(
                r"\b(note|notes|bond|bonds|warrant|warrants|unit|units|preferred|depositary|debenture|rights?)\b",
                name,
                re.IGNORECASE,
            )),
            "is_etf": str(item.get("ETF") or "N").upper() == "Y",
            "source": "nasdaq_trader",
        })
    return rows


def fetch_security_rows() -> list[dict]:
    headers = {"User-Agent": os.getenv("SEC_USER_AGENT", "StockChartApp admin@example.com")}
    combined = {}
    for url, nasdaq in ((NASDAQ_LISTED_URL, True), (OTHER_LISTED_URL, False)):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            for row in parse_symbol_directory(response.text, nasdaq=nasdaq):
                combined[row["symbol"]] = row
        except Exception as exc:
            logger.warning("Symbol directory unavailable at %s: %s", url, exc)
    response = requests.get(SEC_COMPANY_TICKERS_URL, headers=headers, timeout=30)
    response.raise_for_status()
    for row in parse_sec_companies(response.json()):
        previous = combined.get(row["symbol"], {})
        row["is_etf"] = previous.get("is_etf", False)
        row["listing_name"] = previous.get("listing_name") or previous.get("name") or row["name"]
        row["is_primary_equity"] = previous.get("is_primary_equity", True)
        combined[row["symbol"]] = row
    return list(combined.values())


def import_security_master(rows: list[dict], batch_size: int = 500) -> dict:
    url = os.getenv("NEO4J_URL", "bolt://neo4j:7687")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    auth = (user, password) if user and password else None
    with GraphDatabase.driver(url, auth=auth) as driver:
        driver.execute_query("CREATE CONSTRAINT security_symbol IF NOT EXISTS FOR (s:Security) REQUIRE s.symbol IS UNIQUE")
        driver.execute_query("CREATE CONSTRAINT company_key IF NOT EXISTS FOR (c:Company) REQUIRE c.key IS UNIQUE")
        driver.execute_query("CREATE CONSTRAINT exchange_code IF NOT EXISTS FOR (e:Exchange) REQUIRE e.code IS UNIQUE")
        for offset in range(0, len(rows), max(1, batch_size)):
            driver.execute_query(
                """
                UNWIND $rows AS row
                MERGE (s:Security {symbol: row.symbol})
                SET s.name = row.name, s.listing_name = row.listing_name,
                    s.is_etf = row.is_etf, s.is_primary_equity = row.is_primary_equity,
                    s.source = row.source, s.updated_at = datetime()
                MERGE (e:Exchange {code: row.exchange})
                MERGE (s)-[:LISTED_ON]->(e)
                MERGE (c:Company {key: row.company_key})
                SET c.name = row.name, c.cik = row.cik, c.aliases = row.aliases, c.updated_at = datetime()
                MERGE (c)-[:ISSUES]->(s)
                """,
                rows=rows[offset:offset + batch_size],
            )
    return {
        "securities": len(rows),
        "companies": len({row["company_key"] for row in rows}),
        "exchanges": len({row["exchange"] for row in rows}),
    }


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    rows = fetch_security_rows()
    print(json.dumps(import_security_master(rows), indent=2))


if __name__ == "__main__":
    main()
