# backend/test/test_schwab_oauth.py
import pytest
from unittest.mock import patch, MagicMock, mock_open
import os
import time
import json
from urllib.parse import parse_qs, urlparse
import requests

# Adjust path for imports
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from auth import schwab_oauth as so

@pytest.fixture(autouse=True)
def mock_settings():
    # Mock the settings to avoid needing a .env file
    with patch.dict(so.os.environ, {
        "SCHWAB_CLIENT_ID": "test_client_id",
        "SCHWAB_CLIENT_SECRET": "test_client_secret",
        "SCHWAB_REDIRECT_URI": "http://localhost/callback"
    }):
        # Reload the module to pick up the mocked environment variables
        import importlib
        importlib.reload(so)
        yield

def test_build_auth_url():
    url = so.build_auth_url(state="xyz123")
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [so.SCHWAB_CLIENT_ID]
    assert query["redirect_uri"] == [so.SCHWAB_REDIRECT_URI]
    assert query["state"] == ["xyz123"]

def test_basic_auth_b64():
    # This tests the internal helper, which is fine for unit tests
    b64_string = so._basic_auth_b64()
    import base64
    expected = base64.b64encode(b"test_client_id:test_client_secret").decode()
    assert b64_string == expected

@patch('auth.schwab_oauth.requests.post')
def test_exchange_code_for_tokens_success(mock_post):
    # Setup mock response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new_access_token",
        "refresh_token": "new_refresh_token",
        "expires_in": 1800
    }
    mock_post.return_value = mock_response

    with patch.object(so, "TOKEN_DISK_CACHE_ENABLED", True), \
         patch('auth.schwab_oauth.save_tokens_to_cache') as mock_save_tokens:
        tokens = so.exchange_code_for_tokens("test_code")
        
        # Assertions
        assert tokens["access_token"] == "new_access_token"
        assert "expires_at" in tokens
        assert tokens["expires_at"] > time.time()
        
        mock_post.assert_called_once()
        headers = mock_post.call_args.kwargs['headers']
        data = mock_post.call_args.kwargs['data']
        assert headers["Authorization"].startswith("Basic ")
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "test_code"
        
        mock_save_tokens.assert_called_once_with(tokens)

@patch('auth.schwab_oauth.requests.post')
def test_exchange_code_for_tokens_failure(mock_post):
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("401 Unauthorized")
    mock_post.return_value = mock_response

    with pytest.raises(requests.exceptions.HTTPError):
        so.exchange_code_for_tokens("bad_code")

@patch('auth.schwab_oauth.requests.post')
def test_refresh_access_token_success(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "refreshed_access_token",
        "expires_in": 1800
    }
    mock_post.return_value = mock_response

    with patch.object(so, "TOKEN_DISK_CACHE_ENABLED", True), \
         patch('auth.schwab_oauth.save_tokens_to_cache') as mock_save_tokens:
        original_refresh_token = "original_refresh_token"
        tokens = so.refresh_access_token(original_refresh_token)

        assert tokens["access_token"] == "refreshed_access_token"
        # Test that the original refresh token is preserved if not in response
        assert tokens["refresh_token"] == original_refresh_token
        
        mock_post.assert_called_once()
        data = mock_post.call_args.kwargs['data']
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == original_refresh_token
        
        mock_save_tokens.assert_called_once()

def test_save_and_load_tokens_from_cache(tmp_path):
    path = tmp_path / "tokens.json"

    so.save_tokens_to_cache({"access_token": "cached_token"}, str(path))
    tokens = so.load_tokens_from_cache(str(path))

    assert tokens == {"access_token": "cached_token"}
    assert path.stat().st_mode & 0o777 == 0o600

def test_load_tokens_file_not_found():
    with patch("builtins.open", mock_open()) as mock_file:
        mock_file.side_effect = FileNotFoundError
        tokens = so.load_tokens_from_cache()
        assert tokens is None

@patch('auth.schwab_oauth.load_tokens_from_cache')
@patch('auth.schwab_oauth.refresh_access_token')
def test_get_valid_access_token_no_tokens(mock_refresh, mock_load):
    mock_load.return_value = None
    with patch.object(so, "TOKEN_DISK_CACHE_ENABLED", True), \
         pytest.raises(RuntimeError, match="No cached tokens"):
        so.get_valid_access_token()

@patch('auth.schwab_oauth.load_tokens_from_cache')
@patch('auth.schwab_oauth.refresh_access_token')
def test_get_valid_access_token_expired_with_refresh(mock_refresh, mock_load):
    # Token expired 1 hour ago
    mock_load.return_value = {
        "access_token": "expired_token",
        "refresh_token": "valid_refresh_token",
        "expires_at": int(time.time()) - 3600 
    }
    mock_refresh.return_value = {
        "access_token": "newly_refreshed_token",
        "expires_at": int(time.time()) + 1800
    }
    
    with patch.object(so, "TOKEN_DISK_CACHE_ENABLED", True):
        token = so.get_valid_access_token()
    
    assert token == "newly_refreshed_token"
    mock_refresh.assert_called_once_with("valid_refresh_token")

@patch('auth.schwab_oauth.load_tokens_from_cache')
@patch('auth.schwab_oauth.refresh_access_token')
def test_get_valid_access_token_expired_no_refresh(mock_refresh, mock_load):
    mock_load.return_value = {
        "access_token": "expired_token",
        "expires_at": int(time.time()) - 3600
        # No refresh token
    }
    
    with patch.object(so, "TOKEN_DISK_CACHE_ENABLED", True), \
         pytest.raises(RuntimeError, match="no refresh_token available"):
        so.get_valid_access_token()
    
    mock_refresh.assert_not_called()


def test_get_valid_access_token_rejects_legacy_disk_cache_by_default():
    with patch.object(so, "TOKEN_DISK_CACHE_ENABLED", False), \
         pytest.raises(RuntimeError, match="disk cache is disabled"):
        so.get_valid_access_token()
