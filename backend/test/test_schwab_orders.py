# backend/test/test_schwab_orders.py
import pytest
from unittest.mock import patch, MagicMock

# Adjust path for imports
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from schwab.orders import SchwabOrderClient
from schwab.user_preferences import UserPreferences

@pytest.fixture
def mock_user_prefs():
    prefs = MagicMock(spec=UserPreferences)
    prefs.get_streamer_credentials.return_value = {"correlation_id": "test_correl_id"}
    return prefs

@pytest.fixture
@patch('schwab.orders.get_valid_access_token', return_value="dummy_token")
def order_client(mock_get_token, mock_user_prefs):
    return SchwabOrderClient(user_prefs=mock_user_prefs)

@patch.object(SchwabOrderClient, '_request')
def test_get_all_orders(mock_request, order_client):
    from_time = "2025-01-01T00:00:00Z"
    to_time = "2025-01-02T00:00:00Z"
    order_client.get_all_orders(from_time, to_time, max_results=100, status="FILLED")
    
    mock_request.assert_called_once_with("GET", "orders", params={
        "fromEnteredTime": from_time,
        "toEnteredTime": to_time,
        "maxResults": 100,
        "status": "FILLED"
    })

@patch.object(SchwabOrderClient, '_request')
def test_get_orders(mock_request, order_client):
    account_id = "12345"
    from_time = "2025-01-01T00:00:00Z"
    to_time = "2025-01-02T00:00:00Z"
    order_client.get_orders(account_id, from_time, to_time)
    
    mock_request.assert_called_once_with("GET", f"accounts/{account_id}/orders", params={
        "fromEnteredTime": from_time,
        "toEnteredTime": to_time,
        "maxResults": 3000 # Default value
    })

@patch.object(SchwabOrderClient, '_request')
def test_place_order(mock_request, order_client):
    account_id = "12345"
    payload = {"orderType": "MARKET", "session": "NORMAL"}
    order_client.place_order(account_id, payload)
    
    mock_request.assert_called_once_with("POST", f"accounts/{account_id}/orders", json=payload)

@patch.object(SchwabOrderClient, '_request')
def test_get_order(mock_request, order_client):
    account_id = "12345"
    order_id = 987
    order_client.get_order(account_id, order_id)
    
    mock_request.assert_called_once_with("GET", f"accounts/{account_id}/orders/{order_id}")

@patch.object(SchwabOrderClient, '_request')
def test_cancel_order(mock_request, order_client):
    account_id = "12345"
    order_id = 987
    order_client.cancel_order(account_id, order_id)
    
    mock_request.assert_called_once_with("DELETE", f"accounts/{account_id}/orders/{order_id}")

@patch.object(SchwabOrderClient, '_request')
def test_replace_order(mock_request, order_client):
    account_id = "12345"
    order_id = 987
    payload = {"orderType": "LIMIT", "price": 100.0}
    order_client.replace_order(account_id, order_id, payload)
    
    mock_request.assert_called_once_with("PUT", f"accounts/{account_id}/orders/{order_id}", json=payload)

def test_preview_order_not_implemented(order_client):
    with pytest.raises(NotImplementedError):
        order_client.preview_order("12345", {})


@patch("schwab.orders.requests.request")
def test_empty_order_response_preserves_location_and_order_id(mock_request, order_client):
    response = MagicMock()
    response.content = b""
    response.status_code = 201
    response.headers = {
        "Location": "https://api.schwabapi.com/trader/v1/accounts/hash/orders/987654"
    }
    mock_request.return_value = response

    result = order_client.place_order("hash", {"orderType": "MARKET"})

    assert result == {
        "status_code": 201,
        "location": "https://api.schwabapi.com/trader/v1/accounts/hash/orders/987654",
        "order_id": 987654,
    }
    mock_request.assert_called_once_with(
        "POST",
        "https://api.schwabapi.com/trader/v1/accounts/hash/orders",
        headers={
            "Authorization": "Bearer dummy_token",
            "Schwab-Client-CorrelId": "test_correl_id",
            "Content-Type": "application/json",
        },
        params=None,
        json={"orderType": "MARKET"},
        timeout=20,
    )
