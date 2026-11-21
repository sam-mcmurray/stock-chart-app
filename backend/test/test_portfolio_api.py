# backend/test/test_portfolio_api.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# Adjust path for imports
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Before importing the app, mock the dependency
from routers import auth as auth_router
from routers import portfolio as portfolio_router

async def override_require_access_token():
    return "dummy_access_token"

# This import must happen AFTER the dependency override setup
from main import app

app.dependency_overrides[auth_router.require_access_token] = override_require_access_token

client = TestClient(app)

@pytest.fixture
def mock_portfolio_methods():
    with patch('routers.portfolio._get_account') as mock_get_account, \
         patch('routers.portfolio._get_positions') as mock_get_positions, \
         patch('routers.portfolio._get_fills') as mock_get_fills:
        
        mock_get_account.return_value = {
            "equity": 12345.67,
            "cash": 123.45,
            "day_pl": 10.0,
            "sa": {"accountNumber": "123456789"},
            "capabilities": {"trading": True}
        }
        
        mock_get_positions.return_value = [
            {"symbol": "AAPL", "longQuantity": 10, "shortQuantity": 0, "averagePrice": 140.0, "marketPrice": 150.0, "marketValue": 1500.0}
        ]
        
        mock_get_fills.return_value = [
            {"symbol": "TSLA", "side": "BUY", "qty": 5, "price": 200.0, "ts": 1672531200},
            {"symbol": "TSLA", "side": "SELL", "qty": 5, "price": 220.0, "ts": 1672617600},
        ]
        
        yield mock_get_account, mock_get_positions, mock_get_fills

def test_portfolio_summary_endpoint(mock_portfolio_methods):
    response = client.get("/portfolio/summary")
    
    assert response.status_code == 200
    data = response.json()
    
    # Assertions for account data
    assert data["account"]["equity"] == 12345.67
    assert data["account_meta"]["accountNumber"] == "123456789"
    
    # Assertions for positions data
    assert len(data["positions"]) == 1
    assert data["positions"][0]["symbol"] == "AAPL"
    
    # Assertions for fills data
    assert len(data["fills"]) == 2
    assert data["fills"][0]["symbol"] == "TSLA"

    # Assertions for frontend dashboard contract
    assert data["account"]["unrealized_pl"] == 100.0
    assert data["account"]["realized_pl"] == 100.0
    assert data["price_buckets"] == [
        {"bucket": "$100+", "trades": 1, "win_rate": 100.0, "pl": 100.0, "avg_pl": 100.0}
    ]
    assert data["recent_trades"][0]["symbol"] == "TSLA"
    assert data["recent_trades"][0]["pl"] == 100.0
    
    # Check if mocks were called
    mock_get_account, mock_get_positions, mock_get_fills = mock_portfolio_methods
    mock_get_account.assert_called_once()
    mock_get_positions.assert_called_once()
    mock_get_fills.assert_called_once()

def test_portfolio_summary_with_since_param(mock_portfolio_methods):
    since_timestamp = 1672531200 # Jan 1, 2023
    
    response = client.get(f"/portfolio/summary?since={since_timestamp}")
    
    assert response.status_code == 200
    
    # Verify that the 'since' parameter was passed to _get_fills
    _, _, mock_get_fills = mock_portfolio_methods
    mock_get_fills.assert_called_with("dummy_access_token", since_epoch=since_timestamp)

def test_portfolio_summary_trade_limit_caps_recent_trades(mock_portfolio_methods):
    _, _, mock_get_fills = mock_portfolio_methods
    mock_get_fills.return_value = []
    for idx in range(3):
        ts = 1672531200 + idx
        mock_get_fills.return_value.extend([
            {"symbol": f"SYM{idx}", "side": "BUY", "qty": 1, "price": 10.0, "ts": ts},
            {"symbol": f"SYM{idx}", "side": "SELL", "qty": 1, "price": 11.0, "ts": ts + 1},
        ])

    response = client.get("/portfolio/summary?trade_limit=2")

    assert response.status_code == 200
    assert len(response.json()["recent_trades"]) == 2

def test_market_overview_endpoint():
    rows = [
        {"symbol": "$SPX", "name": "S&P 500", "last": 6280.46, "change": 18.24, "percent_change": 0.29, "quote_time": 123},
    ]
    with patch('routers.portfolio._get_market_overview', return_value=rows) as mock_overview:
        response = client.get("/portfolio/market_overview")

    assert response.status_code == 200
    assert response.json() == {"indices": rows}
    mock_overview.assert_called_once_with("dummy_access_token")

def test_market_overview_normalizes_schwab_quotes():
    quote_payload = {
        "$SPX": {"quote": {"lastPrice": 6280.46, "closePrice": 6262.22, "netChange": 18.24, "netPercentChange": 0.2913}},
        "$DJI": {"quote": {"lastPrice": 44620.11, "closePrice": 44662.26, "netChange": -42.15, "netPercentChange": -0.0944}},
    }
    market_client = MagicMock()
    market_client.get_quotes.return_value = quote_payload

    with patch('routers.portfolio._mk_market_client', return_value=market_client):
        from routers.portfolio import _get_market_overview
        import asyncio
        rows = asyncio.run(_get_market_overview("dummy_access_token"))

    assert rows[0]["symbol"] == "$SPX"
    assert rows[0]["last"] == 6280.46
    assert rows[1]["change"] == -42.15
    assert len(rows) == 4
    market_client.get_quotes.assert_called_once_with(["$SPX", "$DJI", "$COMPX", "$RUT"], fields="quote")

def test_price_history_is_anchored_to_current_time():
    import asyncio
    from datetime import datetime, timezone

    market_client = MagicMock()
    market_client.get_price_history.return_value = {"candles": []}
    now = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)

    with (
        patch.object(portfolio_router, "_mk_market_client", return_value=market_client),
        patch.object(portfolio_router, "datetime") as datetime_mock,
    ):
        datetime_mock.now.return_value = now
        asyncio.run(portfolio_router._get_price_history(
            "dummy_access_token",
            "HUT",
            "day",
            1,
            "minute",
            1,
        ))

    market_client.get_price_history.assert_called_once_with(
        "HUT",
        periodType="day",
        period=1,
        frequencyType="minute",
        frequency=1,
        needExtendedHoursData=True,
        endDate=1_784_912_400_000,
    )

def test_price_history_uses_explicit_dates_for_extended_intraday_ranges():
    import asyncio
    from datetime import datetime, timezone

    market_client = MagicMock()
    market_client.get_price_history.return_value = {"candles": []}
    now = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)

    with (
        patch.object(portfolio_router, "_mk_market_client", return_value=market_client),
        patch.object(portfolio_router, "datetime") as datetime_mock,
    ):
        datetime_mock.now.return_value = now
        asyncio.run(portfolio_router._get_price_history(
            "dummy_access_token",
            "HUT",
            "day",
            10,
            "minute",
            5,
            lookback_days=60,
        ))

    market_client.get_price_history.assert_called_once_with(
        "HUT",
        frequencyType="minute",
        frequency=5,
        needExtendedHoursData=True,
        endDate=1_784_912_400_000,
        startDate=1_779_728_400_000,
    )
