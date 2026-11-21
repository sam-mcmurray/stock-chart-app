import re


_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z0-9])")
_EXPLICIT_TICKER_RE = re.compile(
    r"(?:\$([A-Z]{1,5}(?:\.[A-Z])?)\b|"
    r"\b(?:NASDAQ|NYSE|NYSEAMERICAN|AMEX|OTCMKTS|OTCQX|OTCQB)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\b)"
)

# Common all-caps news terms that also happen to be valid exchange symbols.
_TICKER_STOPWORDS = {
    "A", "ACT", "AI", "API", "CCI", "CEO", "CFO", "COO", "CPI", "DES", "ETF",
    "ETFS", "EU", "FDA", "FED", "FOMC", "FOR", "FROM", "FT", "GDP", "I", "ICE",
    "IND", "INC", "IPO", "IT", "LLC", "LTD", "MORE", "NASDAQ", "NEW", "NEWS",
    "NYSE", "P", "PLC", "S", "SEC", "SK", "SOME", "T", "THAT", "THE", "THIS",
    "UAE", "UK", "UN", "USA", "USD", "WITH",
}

def is_plausible_ticker(value: str) -> bool:
    symbol = str(value or "").strip().upper().lstrip("$")
    return bool(_TICKER_RE.fullmatch(symbol)) and symbol not in _TICKER_STOPWORDS


def extract_tickers(text: str) -> list[str]:
    if not text:
        return []
    return sorted({
        match.group(1)
        for match in _TICKER_RE.finditer(text)
        if is_plausible_ticker(match.group(1))
    })


def extract_article_tickers(title: str, explicit_symbols=None) -> list[str]:
    symbols = {
        next(value for value in match.groups() if value)
        for match in _EXPLICIT_TICKER_RE.finditer(title or "")
    }
    for symbol in explicit_symbols or []:
        symbol = str(symbol or "").strip().upper().lstrip("$")
        if is_plausible_ticker(symbol):
            symbols.add(symbol)
    return sorted(symbols)
