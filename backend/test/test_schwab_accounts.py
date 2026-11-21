# backend/test/test_schwab_accounts.py
import pytest
from unittest.mock import patch, MagicMock

# Adjust path for imports
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from schwab.accounts import SchwabAccountClient
from schwab.user_preferences import UserPreferences

@pytest.fixture
def mock_user_prefs():
    # Mock UserPreferences to avoid needing a real one
    prefs = MagicMock(spec=UserPreferences)
    prefs.get_streamer_credentials.return_value = {"correlation_id": "test_correl_id"}
    return prefs

@pytest.fixture
@patch('schwab.accounts.get_valid_access_token', return_value="dummy_token")
def schwab_client_no_eager(mock_get_token, mock_user_prefs):
    # Create a client instance without eager loading for targeted testing
    return SchwabAccountClient(user_prefs=mock_user_prefs, eager_load=False)

# Sample payloads for mocking API responses
SAMPLE_ACCOUNTS_PAYLOAD = [
    {"securitiesAccount": {"accountNumber": "123", "isClosingOnlyRestricted": False, "currentBalances": {"stockBuyingPower": 1000}, "initialBalances": {"cashBalance": 1000}}},
    {"securitiesAccount": {"accountNumber": "456", "isClosingOnlyRestricted": True, "currentBalances": {"stockBuyingPower": 2000, "optionBuyingPower": 500}, "initialBalances": {"cashBalance": 1000, "marginBalance": 1000}}},
]

def test_choose_primary_account(schwab_client_no_eager):
    client = schwab_client_no_eager
    
    # Test case 1: No preferred account, should choose first
    chosen = client._choose_primary(SAMPLE_ACCOUNTS_PAYLOAD)
    assert chosen is not None
    assert chosen["accountNumber"] == "123"

    # Test case 2: Preferred account is set and found
    client.prefer_account_number = "456"
    chosen = client._choose_primary(SAMPLE_ACCOUNTS_PAYLOAD)
    assert chosen is not None
    assert chosen["accountNumber"] == "456"

    # Test case 3: Preferred account not found, should return first
    client.prefer_account_number = "999"
    chosen = client._choose_primary(SAMPLE_ACCOUNTS_PAYLOAD)
    assert chosen is not None
    assert chosen["accountNumber"] == "123"

def test_infer_capabilities_cash_account(schwab_client_no_eager):
    # A simple cash account
    account_data = SAMPLE_ACCOUNTS_PAYLOAD[0]["securitiesAccount"]
    caps = schwab_client_no_eager._infer_capabilities(account_data)
    
    assert caps["has_margin"] is False
    assert caps["options_enabled"] is False
    assert caps["can_short"] is False
    assert caps["closing_only"] is False

def test_infer_capabilities_margin_options_closing_only(schwab_client_no_eager):
    # A more complex account with margin, options, and restrictions
    account_data = SAMPLE_ACCOUNTS_PAYLOAD[1]["securitiesAccount"]
    caps = schwab_client_no_eager._infer_capabilities(account_data)

    assert caps["has_margin"] is True
    assert caps["options_enabled"] is True
    assert caps["can_short"] is False # because closing_only is true
    assert caps["closing_only"] is True

@patch('schwab.accounts.requests.get')
def test_get_all_accounts(mock_get, schwab_client_no_eager):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = SAMPLE_ACCOUNTS_PAYLOAD
    mock_get.return_value = mock_response

    accounts = schwab_client_no_eager.get_all_accounts(include_positions=True)

    assert accounts == SAMPLE_ACCOUNTS_PAYLOAD
    mock_get.assert_called_once()
    url = mock_get.call_args.args[0]
    params = mock_get.call_args.kwargs['params']
    assert url.endswith("/accounts")
    assert params == {"fields": "positions"}

@patch('schwab.accounts.requests.get')
def test_refresh_summary_eager_load(mock_get, mock_user_prefs):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = SAMPLE_ACCOUNTS_PAYLOAD
    mock_get.return_value = mock_response

    # This time, we test the eager loading on init
    with patch('schwab.accounts.get_valid_access_token', return_value="dummy_token"):
        client = SchwabAccountClient(user_prefs=mock_user_prefs, prefer_account_number="123", eager_load=True)

        # Assertions to ensure refresh_summary was called and properties are set
        mock_get.assert_called_once()
        assert client.securities_account is not None
        assert client.securities_account["accountNumber"] == "123"
        assert client.capabilities is not None
        assert client.has_margin is False
        assert client.options_enabled is False
        assert client.closing_only is False
