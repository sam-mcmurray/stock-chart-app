import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from workers.security_master_importer import company_aliases, parse_sec_companies, parse_symbol_directory
from services import security_master
from services.security_master import select_alias_rows


def test_parse_sec_companies_builds_company_security_rows():
    rows = parse_sec_companies({
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[1018724, "Amazon.com, Inc.", "AMZN", "Nasdaq"]],
    })

    assert rows[0]["symbol"] == "AMZN"
    assert rows[0]["company_key"] == "cik:1018724"
    assert "amazon" in rows[0]["aliases"]
    assert "amazon com" in rows[0]["aliases"]


def test_parse_nasdaq_directory_excludes_test_issues():
    text = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "ACME|Acme Corporation|Q|N|N|100|N|N\n"
        "TEST|Test Security|Q|Y|N|100|N|N\n"
        "File Creation Time: 0716202621|||||||\n"
    )

    rows = parse_symbol_directory(text, nasdaq=True)

    assert [row["symbol"] for row in rows] == ["ACME"]
    assert rows[0]["exchange"] == "NASDAQ"


def test_company_aliases_does_not_create_generic_suffix_aliases():
    assert company_aliases("Example Holdings, Inc.") == ["example", "example holdings inc"]


def test_company_aliases_does_not_use_first_word_of_multiword_company():
    assert "meta" not in company_aliases("Meta Platforms, Inc.")
    assert "meta" in company_aliases("Meta Platforms, Inc.", "META")


def test_one_word_company_alias_requires_market_context(monkeypatch):
    monkeypatch.setattr(security_master, "_load_alias_rows", lambda: [("west", "WEST"), ("meta", "META"), ("cheer", "CHR")])
    security_master._CACHE["index"] = {
        "west": [("west", "WEST")],
        "meta": [("meta", "META")],
        "cheer": [("cheer", "CHR")],
    }

    assert security_master.resolve_company_tickers("Conflict expands in the West Bank") == []
    assert security_master.resolve_company_tickers("Meta is one of three stocks to buy") == ["META"]
    assert security_master.resolve_company_tickers("investors cheer company earnings") == []


def test_multiword_company_name_matches_only_corporate_suffix_variants(monkeypatch):
    rows = [
        ("novo nordisk a s", "NVO"),
        ("eli lilly and company", "LLY"),
    ]
    monkeypatch.setattr(security_master, "_load_alias_rows", lambda: rows)
    security_master._CACHE["index"] = {
        "novo": [rows[0]],
        "nordisk": [rows[0]],
        "eli": [rows[1]],
        "lilly": [rows[1]],
    }

    assert security_master.resolve_company_tickers("Novo Nordisk") == ["NVO"]
    assert security_master.resolve_company_tickers("Eli Lilly Inc") == ["LLY"]


def test_exact_company_name_rejects_shorter_similarly_named_issuer(monkeypatch):
    rows = [("great eastern", "GEHDF")]
    monkeypatch.setattr(security_master, "_load_alias_rows", lambda: rows)
    security_master._CACHE["index"] = {
        "great": [rows[0]],
        "eastern": [rows[0]],
    }

    assert security_master.resolve_company_tickers("Great Eastern Shipping") == ["GEHDF"]
    assert security_master.resolve_company_tickers(
        "Great Eastern Shipping",
        exact_company_name=True,
    ) == []


def test_exact_full_corporate_name_can_have_single_word_core(monkeypatch):
    rows = [("microsoft corporation", "MSFT"), ("nvidia corporation", "NVDA"), ("west", "WEST")]
    monkeypatch.setattr(security_master, "_load_alias_rows", lambda: rows)
    monkeypatch.setitem(security_master._CACHE, "index", {
        "microsoft": [rows[0]], "nvidia": [rows[1]], "west": [rows[2]],
    })
    assert security_master.resolve_company_tickers("Microsoft Corporation", exact_company_name=True) == ["MSFT"]
    assert security_master.resolve_company_tickers("NVIDIA Corporation", exact_company_name=True) == ["NVDA"]
    assert security_master.resolve_company_tickers("West Bank", exact_company_name=True) == []
    assert security_master.resolve_company_tickers("West", exact_company_name=True) == []
    assert security_master.resolve_company_tickers("West Corporation", exact_company_name=True) == []
    assert security_master.resolve_company_tickers("Microsoft Security Corporation", exact_company_name=True) == []


def test_select_alias_rows_removes_generic_ambiguous_and_duplicate_otc_aliases():
    rows = [
        {"company_key": "nbhc", "alias": "national bank", "symbol": "NBHC", "exchange": "NYSE"},
        {"company_key": "gs", "alias": "goldman sachs", "symbol": "GS", "exchange": "NYSE"},
        {"company_key": "gs", "alias": "goldman sachs", "symbol": "GSCE", "exchange": "OTC"},
        {"company_key": "one", "alias": "acme", "symbol": "ACM", "exchange": "NYSE"},
        {"company_key": "two", "alias": "acme", "symbol": "ACME", "exchange": "NASDAQ"},
        {"company_key": "alphabet", "alias": "alphabet", "symbol": "GOOG", "exchange": "NASDAQ"},
        {"company_key": "alphabet", "alias": "alphabet", "symbol": "GOOGL", "exchange": "NASDAQ"},
        {"company_key": "freddie", "alias": "freddie mac", "symbol": "FMCC", "exchange": "OTC"},
        {"company_key": "freddie", "alias": "freddie mac", "symbol": "FMCCI", "exchange": "OTC"},
        {"company_key": "ameren-il", "alias": "ameren illinois", "symbol": "AILIH", "exchange": "OTC"},
        {"company_key": "ameren-il", "alias": "ameren illinois", "symbol": "AILIN", "exchange": "OTC"},
    ]

    assert select_alias_rows(rows) == [
        ("alphabet", "GOOG"),
        ("alphabet", "GOOGL"),
        ("freddie mac", "FMCC"),
        ("goldman sachs", "GS"),
    ]


def test_security_company_identity_uses_cached_master_name(monkeypatch):
    monkeypatch.setattr(security_master, "_load_alias_rows", lambda: [])
    security_master._CACHE["companies"] = {
        "NVDA": {"name": "NVIDIA Corporation", "exchange": "NASDAQ", "source": "security_master"}
    }

    assert security_master.security_company_identity("nvda") == {
        "name": "NVIDIA Corporation",
        "exchange": "NASDAQ",
        "source": "security_master",
    }
