"""Archive SEC filings and extract explicit, dated nonaffiliate share counts.

This deliberately narrow cover-page parser abstains on unfamiliar wording or
multiple share classes. It never treats shares outstanding as public float.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from bs4 import BeautifulSoup
from dotenv import dotenv_values
import requests

from drl_agent.dataset import encode_dataset
from drl_agent.historical_news_join import _verify


def _digest(value):
    return hashlib.sha256(encode_dataset(value).encode()).hexdigest()


def cache_filing(history, accession, filename, output, *, user_agent=None, get=requests.get):
    _verify(history, "sec-share-history-v1", "history_sha256")
    if accession not in {r["accession"] for r in history["records"]}:
        raise ValueError("Accession missing from verified history")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.html?", filename):
        raise ValueError("Expected a filing HTML basename")
    url = (f"https://www.sec.gov/Archives/edgar/data/{history['cik']}/"
           f"{accession.replace('-', '')}/{filename}")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{accession}_{filename}_filing.json"
    lock = output / ("." + target.name + ".lock")
    with lock.open("x"):
        pass
    temporary = None
    try:
        if target.exists():
            result = json.loads(target.read_text())
            _verify(result, "sec-filing-cache-v1", "archive_sha256")
            if result["url"] != url or result["accession"] != accession or result["cik"] != history["cik"]:
                raise ValueError("Filing cache identity mismatch")
            return result, True
        if (not user_agent or not re.search(r"\S+@\S+\.\S+", user_agent)
                or "example.com" in user_agent.lower() or "\n" in user_agent or "\r" in user_agent):
            raise ValueError("Set SEC_USER_AGENT with a real contact email")
        with get(url, headers={"User-Agent": user_agent, "Accept": "text/html"},
                 timeout=(15, 60), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError(f"SEC filing returned HTTP {response.status_code}; no automatic retries")
            chunks, size = [], 0
            for chunk in response.iter_content(1024 * 1024):
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise ValueError("Filing exceeds size budget")
                chunks.append(chunk)
        html = b"".join(chunks).decode("utf-8-sig")
        if "<html" not in html.lower():
            raise ValueError("Expected HTML filing")
        result = {"version": "sec-filing-cache-v1", "cik": history["cik"],
                  "accession": accession, "url": url, "html": html,
                  "archived_at": datetime.now(timezone.utc).isoformat()}
        result["archive_sha256"] = _digest(result)
        fd, name = tempfile.mkstemp(prefix=".sec_filing_", suffix=".part", dir=output)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encode_dataset(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        return result, False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()


def _cover_price_estimates(text, history, filing, symbol, equity_class):
    pattern = re.compile(
        r"aggregate market value of the [^.]{0,100}?common stock outstanding, "
        r"other than securities held by persons who may be deemed affiliates [^.]{0,150}?"
        r"closing sales price of \$\s*(?P<price>\d[\d,]*(?:\.\d+)?) on "
        r"(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})[^.]{0,250}?"
        r"was (?:approximately )?\$\s*(?P<usd>\d[\d,]*(?:\.\d+)?)\s*\.", re.I)
    date_first = re.compile(
        r"As of (?P<date>[A-Z][a-z]+ \d{1,2}, \d{4}), the aggregate market value of the "
        r"(?:voting and non-voting )?common (?:shares|stock) held by non-?affiliates of the registrant, "
        r"based on the closing price of \$\s*(?P<price>\d[\d,]*(?:\.\d+)?) per share"
        r"[^.]{0,250}? was \$\s*(?P<usd>\d[\d,]*(?:\.\d+)?)(?=\s*(?:[.(]|$))", re.I)
    matches = [match for candidate in (pattern, date_first) for match in candidate.finditer(text[:10000])]
    if len(matches) != 1:
        return []
    match = matches[0]
    try:
        measured = datetime.strptime(match["date"], "%B %d, %Y").date().isoformat()
        price = Decimal(match["price"].replace(",", ""))
        dollars = Decimal(match["usd"].replace(",", ""))
    except (ValueError, ArithmeticError):
        return []
    facts = [r for r in history["records"] if r["kind"] == "public_float_value"
             and r["accession"] == filing["accession"] and r["effective_date"] == measured
             and Decimal(str(r["value"])) == dollars]
    if len(facts) != 1 or price <= 0 or dollars <= 0:
        return []
    row = {"symbol": symbol, "cik": history["cik"], "equity_class": equity_class,
           "value": float(dollars / price), "unit": "shares",
           "basis": "sec_cover_value_divided_by_matching_price", "reported_not_estimated": False,
           "effective_date": measured, "available_ts": facts[0]["available_ts"],
           "public_float_usd": str(dollars), "measurement_price_usd": str(price),
           "source_url": filing["url"], "source_record_id": facts[0]["record_id"],
           "filing_archive_sha256": filing["archive_sha256"], "evidence_text": match[0],
           "entry_eligible": False, "corporate_actions_applied": False}
    row["record_id"] = _digest(row)
    return [row]


def extract_float(history, filing):
    _verify(history, "sec-share-history-v1", "history_sha256")
    _verify(filing, "sec-filing-cache-v1", "archive_sha256")
    if filing["cik"] != history["cik"]:
        raise ValueError("Filing CIK mismatch")
    soup = BeautifulSoup(filing["html"], "html.parser")
    def facts(name):
        return {" ".join(tag.get_text(" ", strip=True).split())
                for tag in soup.find_all(attrs={"name": name})}
    symbols = facts("dei:TradingSymbol")
    classes = facts("dei:Security12bTitle")
    identities = facts("dei:EntityCentralIndexKey")
    records, reasons = [], []
    if (not identities or any(not s.isdigit() or int(s) != history["cik"] for s in identities)):
        reasons.append("filing_cik_unverified")
    if len(symbols) != 1 or len(classes) != 1:
        reasons.append("single_equity_class_unverified")
    elif not re.search(r"common (?:stock|shares)", next(iter(classes)), re.I):
        reasons.append("common_equity_class_unverified")
    # Match a complete statement, not arbitrary numbers elsewhere in the filing.
    pattern = re.compile(
        r"aggregate market value of the (?P<shares>\d{1,3}(?:,\d{3})+|\d+) shares of "
        r"(?:voting stock|common stock|common shares) held by non-?affiliates "
        r"(?P<body>[^.]{1,1000}?) was \$\s*(?P<usd>\d{1,3}(?:,\d{3})+|\d+)(?:\.00)?\s*\.", re.I)
    parenthetical = re.compile(
        r"aggregate market value of the voting and non-voting common equity held by non-?affiliates "
        r"of the registrant on (?P<body>[A-Z][a-z]+ \d{1,2}, \d{4}), computed by reference to the price "
        r"at which the registrant['\u2019]s (?P<equity_class>Class [A-Z] common stock) was last sold "
        r"on [^.]{1,100}? on such date was \$\s*(?P<usd>\d{1,3}(?:,\d{3})+|\d+) "
        r"\((?P<shares>\d{1,3}(?:,\d{3})+|\d+) shares at a closing price per share of "
        r"\$\s*(?P<price>\d+(?:\.\d+)?)\)\s*\.", re.I)
    # Read identity facts above before removing hidden XBRL contexts from cover text.
    for header in soup.find_all("ix:header"):
        header.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    matches = sorted([*pattern.finditer(text), *parenthetical.finditer(text[:10000])],
                     key=lambda match: match.start())
    if not matches:
        reasons.append("explicit_nonaffiliate_count_not_found")
    for match in matches if not reasons else []:
        tail = text[match.end():match.end() + 160]
        is_parenthetical = match.re is parenthetical
        if is_parenthetical:
            if match["equity_class"].casefold() != next(iter(classes)).casefold():
                reasons.append("additional_equity_scope_unverified")
                continue
            price = Decimal(match["price"])
            implied_value = int(match["shares"].replace(",", "")) * price
            if price <= 0 or abs(implied_value - Decimal(match["usd"].replace(",", ""))) > Decimal("0.5"):
                reasons.append("reported_count_price_value_mismatch")
                continue
        elif not re.match(r"\s*[^.]{0,80}\bhas no non-voting common equity\.", tail, re.I):
            reasons.append("additional_equity_scope_unverified")
            continue
        days = re.findall(r"(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}", match["body"])
        try:
            measured = {datetime.strptime(day, "%B %d, %Y").date().isoformat() for day in days}
        except ValueError:
            measured = set()
        candidates = [r for r in history["records"] if r["kind"] == "public_float_value"
                      and r["accession"] == filing["accession"] and r["effective_date"] in measured
                      and r["value"] == int(match["usd"].replace(",", ""))]
        if len(measured) != 1 or len(candidates) != 1:
            reasons.append("measurement_date_or_dollar_value_mismatch")
            continue
        fact = candidates[0]
        count = int(match["shares"].replace(",", ""))
        if count <= 0:
            reasons.append("invalid_share_count")
            continue
        row = {"symbol": next(iter(symbols)), "cik": history["cik"],
               "equity_class": next(iter(classes)), "value": count, "unit": "shares",
               "basis": "reported_nonaffiliate_shares", "reported_not_estimated": True,
               "effective_date": fact["effective_date"], "available_ts": fact["available_ts"],
               "availability_basis": fact["availability_basis"], "accession": filing["accession"],
               "source_record_id": fact["record_id"], "source_url": filing["url"],
               "filing_archive_sha256": filing["archive_sha256"],
               "evidence_text": match[0] if is_parenthetical else match[0] + tail.split(".")[0] + "."}
        row["record_id"] = _digest(row)
        if row not in records:
            records.append(row)
    if len({(r["effective_date"], r["value"]) for r in records}) > 1:
        reasons.append("conflicting_cover_statements")
        records = []
    estimates = []
    if reasons == ["explicit_nonaffiliate_count_not_found"]:
        estimates = _cover_price_estimates(text, history, filing, next(iter(symbols)), next(iter(classes)))
    result = {"version": "sec-filing-float-v1", "records": records, "estimates": estimates,
              "abstentions": sorted(set(reasons)), "source_history_sha256": history["history_sha256"],
              "filing_archive_sha256": filing["archive_sha256"], "training_ready": False,
              "limitations": ["Reported nonaffiliate shares are a dated public-float proxy, not current tradable supply",
                  "No automatic carry-forward across splits, dilution, or other share-class changes",
                  "Unrecognized wording and multi-class issuers require another extraction path"]}
    result["float_sha256"] = _digest(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--accession", required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    history = json.loads(args.history.read_text())
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    filing, reused = cache_filing(history, args.accession, args.filename, args.output,
        user_agent=os.getenv("SEC_USER_AGENT") or env.get("SEC_USER_AGENT"))
    result = extract_float(history, filing)
    target = args.output / f"{args.accession}_{result['float_sha256'][:16]}_float.json"
    if target.exists():
        if json.loads(target.read_text()) != result:
            raise ValueError("Existing extraction differs; not overwritten")
    else:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(encode_dataset(result) + "\n")
    print(encode_dataset({"file": str(target), "reused": reused,
                         "records": len(result["records"]), "estimates": len(result["estimates"]),
                         "abstentions": result["abstentions"]}))


if __name__ == "__main__":
    main()
