# backend/test/test_schwab_market_data.py
import pytest
from unittest.mock import patch, MagicMock

# Adjust path for imports
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from schwab.market_data import SchwabMarketDataClient
from schwab.user_preferences import UserPreferences

@pytest.fixture
def mock_user_prefs():
    prefs = MagicMock(spec=UserPreferences)
    prefs.get_streamer_credentials.return_value = {"correlation_id": "test_correl_id"}
    return prefs

@pytest.fixture
@patch('schwab.market_data.get_valid_access_token', return_value="dummy_token")
def market_data_client(mock_get_token, mock_user_prefs):
    return SchwabMarketDataClient(user_prefs=mock_user_prefs)

@patch.object(SchwabMarketDataClient, '_get')
def test_get_quotes(mock_get, market_data_client):
    symbols = ["AAPL", "GOOG"]
    market_data_client.get_quotes(symbols, fields="quote", indicative=True)
    
    mock_get.assert_called_once_with("quotes", {
        "symbols": "AAPL,GOOG",
        "fields": "quote",
        "indicative": "true"
    })

@patch.object(SchwabMarketDataClient, '_get')
def test_get_quote(mock_get, market_data_client):
    symbol = "MSFT"
    market_data_client.get_quote(symbol, fields="fundamental")
    
    mock_get.assert_called_once_with(f"{symbol}/quotes", {"fields": "fundamental"})

@patch.object(SchwabMarketDataClient, 'get_quote')
def test_get_last_price(mock_get_quote, market_data_client):
    mock_get_quote.return_value = {"MSFT": {"quote": {"lastPrice": 412.34}}}
    assert market_data_client.get_last_price("MSFT") == 412.34

@patch.object(SchwabMarketDataClient, '_get')
def test_get_option_chain(mock_get, market_data_client):
    symbol = "TSLA"
    kwargs = {"strikeCount": 10, "strategy": "SINGLE"}
    market_data_client.get_option_chain(symbol, **kwargs)
    
    mock_get.assert_called_once_with("chains", {"symbol": symbol, **kwargs})

@patch.object(SchwabMarketDataClient, '_get')
def test_get_price_history(mock_get, market_data_client):
    symbol = "NVDA"
    kwargs = {"periodType": "day", "period": 1}
    market_data_client.get_price_history(symbol, **kwargs)
    
    mock_get.assert_called_once_with("pricehistory", {"symbol": symbol, **kwargs})

@patch.object(SchwabMarketDataClient, 'get_price_history')
def test_get_price_history_frame(mock_get_price_history, market_data_client):
    mock_get_price_history.return_value = {
        "candles": [
            {"datetime": 1716998400000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1000}
        ]
    }
    df = market_data_client.get_price_history_frame("TEST", periodType="day", period=1)
    assert len(df) == 1
    assert float(df["c"].iloc[0]) == 1.5

@patch.object(SchwabMarketDataClient, 'get_price_history_frame')
def test_get_intraday_frame_latest_day(mock_get_frame, market_data_client):
    import pandas as pd
    mock_get_frame.return_value = pd.DataFrame({
        "t": pd.to_datetime(["2026-05-28T14:00:00Z", "2026-05-29T04:30:00Z", "2026-05-29T14:00:00Z", "2026-05-29T14:05:00Z"], utc=True),
        "o": [1, 1.5, 2, 3],
        "h": [1, 1.5, 2, 3],
        "l": [1, 1.5, 2, 3],
        "c": [1, 1.5, 2, 3],
        "v": [10, 15, 20, 30],
    })
    df = market_data_client.get_intraday_frame("TEST")
    assert len(df) == 2
    assert float(df["c"].iloc[-1]) == 3.0

@patch.object(SchwabMarketDataClient, '_get')
def test_get_market_movers(mock_get, market_data_client):
    # Mock the return value of _get to test the parsing logic
    mock_get.return_value = {"screeners": [{"symbol": "MOVER1"}, {"symbol": "MOVER2"}]}
    
    movers = market_data_client.get_market_movers("NASDAQ", sort="VOLUME", frequency=60)
    
    assert movers == [{"symbol": "MOVER1"}, {"symbol": "MOVER2"}]
    mock_get.assert_called_once_with("movers/NASDAQ", {"sort": "VOLUME", "frequency": 60})

@patch.object(SchwabMarketDataClient, 'get_market_movers')
def test_get_mover_symbols(mock_get_market_movers, market_data_client):
    mock_get_market_movers.return_value = [
        {"symbol": "MOVER1"},
        {"symbol": "MOVER2"},
        {"no_symbol_key": "some_value"}
    ]
    
    symbols = market_data_client.get_mover_symbols("NYSE")
    
    assert symbols == ["MOVER1", "MOVER2"]
    mock_get_market_movers.assert_called_once_with("NYSE", "PERCENT_CHANGE_UP", 0)

@patch.object(SchwabMarketDataClient, '_get')
def test_get_market_hours(mock_get, market_data_client):
    markets = ["equity", "option"]
    date = "2025-12-25"
    market_data_client.get_market_hours(markets, date)
    
    mock_get.assert_called_once_with("markets", {"markets": "equity,option", "date": date})

@patch.object(SchwabMarketDataClient, '_get')
def test_get_instruments(mock_get, market_data_client):
    symbol = "AAPL"
    projection = "fundamental"
    market_data_client.get_instruments(symbol, projection)
    
    mock_get.assert_called_once_with("instruments", {"symbol": symbol, "projection": projection})

@patch.object(SchwabMarketDataClient, '_get')
def test_get_instrument_by_cusip(mock_get, market_data_client):
    cusip = "037833100" # CUSIP for Apple
    market_data_client.get_instrument_by_cusip(cusip)
    
    mock_get.assert_called_once_with(f"instruments/{cusip}")
