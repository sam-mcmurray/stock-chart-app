# backend/test/test_schwab_transactions.py
import pytest
from unittest.mock import patch, MagicMock

# Adjust path for imports
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from schwab.transactions import SchwabTransactionClient
from schwab.user_preferences import UserPreferences

@pytest.fixture
def mock_user_prefs():
    prefs = MagicMock(spec=UserPreferences)
    prefs.get_streamer_credentials.return_value = {"correlation_id": "test_correl_id"}
    return prefs

@pytest.fixture
@patch('schwab.transactions.get_valid_access_token', return_value="dummy_token")
def transaction_client(mock_get_token, mock_user_prefs):
    return SchwabTransactionClient(user_prefs=mock_user_prefs)

@patch.object(SchwabTransactionClient, '_request')
def test_get_transactions(mock_request, transaction_client):
    account_id = "12345"
    start_date = "2025-01-01T00:00:00Z"
    end_date = "2025-02-01T00:00:00Z"
    types = "TRADE"
    
    transaction_client.get_transactions(account_id, start_date, end_date, types)
    
    mock_request.assert_called_once_with("GET", f"accounts/{account_id}/transactions", params={
        "startDate": start_date,
        "endDate": end_date,
        "types": types
    })

@patch.object(SchwabTransactionClient, '_request')
def test_get_transactions_with_symbol(mock_request, transaction_client):
    account_id = "12345"
    start_date = "2025-01-01T00:00:00Z"
    end_date = "2025-02-01T00:00:00Z"
    types = "TRADE"
    symbol = "AAPL"
    
    transaction_client.get_transactions(account_id, start_date, end_date, types, symbol=symbol)
    
    mock_request.assert_called_once_with("GET", f"accounts/{account_id}/transactions", params={
        "startDate": start_date,
        "endDate": end_date,
        "types": types,
        "symbol": symbol
    })

@patch.object(SchwabTransactionClient, '_request')
def test_get_transaction(mock_request, transaction_client):
    account_id = "12345"
    transaction_id = 98765
    
    transaction_client.get_transaction(account_id, transaction_id)
    
    mock_request.assert_called_once_with("GET", f"accounts/{account_id}/transactions/{transaction_id}")


def test_transaction_client_accepts_explicit_access_token(mock_user_prefs):
    client = SchwabTransactionClient(user_prefs=mock_user_prefs, access_token="session_token")

    assert client.access_token == "session_token"
