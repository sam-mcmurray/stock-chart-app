# backend/test/test_schwab_user_preferences.py
import pytest
from unittest.mock import patch, MagicMock

# Adjust path for imports
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from schwab.user_preferences import UserPreferences

SAMPLE_PREFERENCES_PAYLOAD = {
    "accounts": [
        {"accountNumber": "111", "primaryAccount": False},
        {"accountNumber": "222", "primaryAccount": True},
        {"accountNumber": "333", "primaryAccount": False},
    ],
    "streamerInfo": [
        {
            "streamerSocketUrl": "wss://example.com/ws",
            "schwabClientCustomerId": "customer1",
            "schwabClientCorrelId": "correl1",
            "schwabClientChannel": "channel1",
            "schwabClientFunctionId": "function1",
        }
    ],
    "offers": [
        {"level2Permissions": "true"}
    ]
}

@pytest.fixture
def mock_get_user_preferences():
    # This fixture patches the method that makes the real HTTP call
    with patch.object(UserPreferences, 'get_user_preferences', return_value=SAMPLE_PREFERENCES_PAYLOAD) as mock_method:
        yield mock_method

def test_user_preferences_initialization(mock_get_user_preferences):
    # The __init__ method calls the patched get_user_preferences
    prefs = UserPreferences()
    
    # Assert that the method was called once during initialization
    mock_get_user_preferences.assert_called_once()
    
    # Assert that the internal attributes are set correctly
    assert len(prefs.accounts) == 3
    assert prefs.streamer_info is not None
    assert prefs.streamer_info["streamerSocketUrl"] == "wss://example.com/ws"
    assert len(prefs.offers) == 1

def test_get_primary_account(mock_get_user_preferences):
    prefs = UserPreferences()
    primary = prefs.get_primary_account()
    
    assert primary is not None
    assert primary["accountNumber"] == "222"
    assert primary["primaryAccount"] is True

def test_get_primary_account_not_found(mock_get_user_preferences):
    # Override the mock to return data with no primary account
    no_primary_payload = {**SAMPLE_PREFERENCES_PAYLOAD, "accounts": [{"primaryAccount": False}]}
    mock_get_user_preferences.return_value = no_primary_payload
    
    prefs = UserPreferences()
    primary = prefs.get_primary_account()
    assert primary is None

def test_get_streamer_credentials(mock_get_user_preferences):
    prefs = UserPreferences()
    creds = prefs.get_streamer_credentials()
    
    assert creds["socket_url"] == "wss://example.com/ws"
    assert creds["customer_id"] == "customer1"
    assert creds["correlation_id"] == "correl1"
    assert creds["channel"] == "channel1"
    assert creds["function_id"] == "function1"

def test_get_streamer_credentials_missing(mock_get_user_preferences):
    # Override the mock to return data with no streamer info
    no_streamer_payload = {**SAMPLE_PREFERENCES_PAYLOAD, "streamerInfo": []}
    mock_get_user_preferences.return_value = no_streamer_payload
    
    with pytest.raises(ValueError, match="streamerInfo is missing"):
        # The error is raised on access, not init
        prefs = UserPreferences()
        prefs.get_streamer_credentials()

def test_get_offer_permissions(mock_get_user_preferences):
    prefs = UserPreferences()
    offers = prefs.get_offer_permissions()
    
    assert offers is not None
    assert offers["level2Permissions"] == "true"
