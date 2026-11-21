# data/schwab/schwab_market_data.py

import os
import uuid
from datetime import time
import requests
import pandas as pd
from auth.schwab_oauth import get_valid_access_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

class SchwabMarketDataClient:
    def __init__(
        self,
        user_prefs,
        access_token: str | None = None,
        timeout_seconds: float | None = None,
    ):
        self.creds = user_prefs.get_streamer_credentials()
        self.access_token = (
            access_token
            or getattr(user_prefs, "access_token", None)
            or get_valid_access_token()
        )
        self.correlation_id = self.creds.get("correlation_id")
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("SCHWAB_HTTP_TIMEOUT_SECONDS", "20")
        )

    def _get(self, endpoint: str, params=None):
        url = f"{BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Schwab-Client-CorrelId": self.correlation_id or str(uuid.uuid4())
        }
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    # ✅ Quotes
    def get_quotes(self, symbols: list[str], fields: str = None, indicative: bool = False):
        params = {"symbols": ",".join(symbols)}
        if fields:
            params["fields"] = fields
        if indicative:
            params["indicative"] = str(indicative).lower()
        return self._get("quotes", params)

    def get_quote(self, symbol: str, fields: str = None):
        params = {"fields": fields} if fields else None
        return self._get(f"{symbol}/quotes", params)

    def get_last_price(self, symbol: str) -> float:
        data = self.get_quote(symbol)
        quote = data.get(symbol.upper()) if isinstance(data, dict) else None
        quote = quote or data
        quote_data = quote.get("quote") if isinstance(quote, dict) else {}
        for key in ("lastPrice", "mark", "regularMarketLastPrice", "closePrice"):
            try:
                value = float(quote_data.get(key) or quote.get(key) or 0)
                if value > 0:
                    return value
            except Exception:
                continue
        return 0.0

    # ✅ Option Chains
    def get_option_chain(self, symbol: str, **kwargs):
        params = {"symbol": symbol, **kwargs}
        return self._get("chains", params)

    def get_option_expiration_chain(self, symbol: str):
        return self._get("expirationchain", {"symbol": symbol})

    # ✅ Price History
    def get_price_history(self, symbol: str, **kwargs):
        params = {"symbol": symbol, **kwargs}
        return self._get("pricehistory", params)

    def get_price_history_frame(self, symbol: str, **kwargs):
        data = self.get_price_history(symbol, **kwargs)
        candles = data.get("candles") or []
        if not candles:
            return pd.DataFrame()
        return pd.DataFrame({
            "t": pd.to_datetime([c.get("datetime") for c in candles], unit="ms", utc=True),
            "o": [c.get("open") for c in candles],
            "h": [c.get("high") for c in candles],
            "l": [c.get("low") for c in candles],
            "c": [c.get("close") for c in candles],
            "v": [c.get("volume") for c in candles],
        })

    def get_daily_frame(self, symbol: str, months: int = 3):
        return self.get_price_history_frame(
            symbol,
            periodType="month",
            period=months,
            frequencyType="daily",
            frequency=1,
            needExtendedHoursData=True,
        )

    def get_intraday_frame(self, symbol: str, days: int = 2, frequency: int = 5):
        df = self.get_price_history_frame(
            symbol,
            periodType="day",
            period=days,
            frequencyType="minute",
            frequency=frequency,
            needExtendedHoursData=True,
        )
        if df.empty or "t" not in df.columns:
            return df
        market_times = df["t"].dt.tz_convert("America/New_York")
        supported_session = (market_times.dt.time >= time(4, 0)) & (market_times.dt.time < time(20, 0))
        df = df[supported_session].copy()
        if df.empty:
            return df
        market_dates = df["t"].dt.tz_convert("America/New_York").dt.date
        latest_day = market_dates.max()
        return df[market_dates == latest_day].reset_index(drop=True)

    # ✅ Movers
    def get_market_movers(self, symbol_id="NASDAQ", sort="PERCENT_CHANGE_UP", frequency=0):
        params = {"sort": sort, "frequency": frequency}
        return self._get(f"movers/{symbol_id}", params).get("screeners", [])

    def get_mover_symbols(self, symbol_id="NASDAQ", sort="PERCENT_CHANGE_UP", frequency=0):
        movers = self.get_market_movers(symbol_id, sort, frequency)
        return [entry["symbol"] for entry in movers if "symbol" in entry]

    # ✅ Market Hours
    def get_market_hours(self, markets: list[str], date: str = None):
        params = {"markets": ",".join(markets)}
        if date:
            params["date"] = date
        return self._get("markets", params)

    def get_market_hours_for(self, market_id: str, date: str = None):
        params = {"date": date} if date else None
        return self._get(f"markets/{market_id}", params)

    # ✅ Instruments
    def get_instruments(self, symbol: str, projection: str = "symbol-search"):
        return self._get("instruments", {
            "symbol": symbol,
            "projection": projection
        })

    def get_instrument_by_cusip(self, cusip_id: str):
        return self._get(f"instruments/{cusip_id}")
