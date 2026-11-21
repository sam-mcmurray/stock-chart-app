# backend/test/test_session_store.py
import pytest
from unittest.mock import patch, MagicMock
import os
import time
import json
from cryptography.fernet import Fernet

# Adjust path for imports
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Mock the environment variable for encryption BEFORE importing the module
TEST_ENC_KEY = Fernet.generate_key()
with patch.dict(os.environ, {"REDIS_TOKEN_ENC_KEY": TEST_ENC_KEY.decode()}):
    from auth import session_store as ss
    import importlib
    importlib.reload(ss) # Reload to apply the mocked env var

@pytest.fixture
def mock_redis():
    return MagicMock()

@pytest.fixture
def sample_tokens():
    return {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "expires_in": 1800
    }

def test_enc_dec_with_key():
    """Test encryption and decryption when a key is present."""
    data = {"hello": "world"}
    encrypted = ss._enc(data)
    assert encrypted != json.dumps(data) # Should be encrypted
    decrypted = ss._dec(encrypted)
    assert decrypted == data

def test_dec_invalid_token_with_key():
    """Test that tampered data returns an empty dict."""
    decrypted = ss._dec("invalid_encrypted_string")
    assert decrypted == {}

@patch.dict(os.environ, {"REDIS_TOKEN_ENC_KEY": ""})
def test_enc_dec_without_key():
    """Test behavior when no encryption key is set."""
    # Reload the module to pick up the new (empty) env var
    import importlib
    importlib.reload(ss)
    
    data = {"hello": "world"}
    encrypted = ss._enc(data)
    assert encrypted == json.dumps(data) # Should be plain JSON
    decrypted = ss._dec(encrypted)
    assert decrypted == data
    
    # Restore the original module state after test
    with patch.dict(os.environ, {"REDIS_TOKEN_ENC_KEY": TEST_ENC_KEY.decode()}):
        importlib.reload(ss)

def test_save_tokens(mock_redis, sample_tokens):
    sid = "session123"
    ss.save_tokens(mock_redis, sid, sample_tokens)
    
    # Check that r.set was called
    mock_redis.set.assert_called_once()
    
    # Check the arguments passed to r.set
    args, kwargs = mock_redis.set.call_args
    assert args[0] == f"tok:{sid}" # Check key
    
    # Decrypt the value to verify its contents
    encrypted_value = args[1]
    decrypted_tokens = ss._dec(encrypted_value)
    
    assert decrypted_tokens["access_token"] == sample_tokens["access_token"]
    assert "expires_at" in decrypted_tokens
    
    # Check TTL
    assert "ex" in kwargs
    assert kwargs["ex"] == ss.SESSION_TOKEN_TTL_SECONDS

def test_load_tokens(mock_redis, sample_tokens):
    sid = "session123"
    
    # Mock r.get to return the encrypted version of our sample tokens
    encrypted_tokens = ss._enc(sample_tokens)
    mock_redis.get.return_value = encrypted_tokens
    
    tokens = ss.load_tokens(mock_redis, sid)
    
    mock_redis.get.assert_called_once_with(f"tok:{sid}")
    assert tokens is not None
    assert tokens["access_token"] == sample_tokens["access_token"]

def test_load_tokens_not_found(mock_redis):
    mock_redis.get.return_value = None
    tokens = ss.load_tokens(mock_redis, "nonexistent_sid")
    assert tokens is None

def test_save_and_load_trading_tokens(mock_redis, sample_tokens):
    ss.save_trading_tokens(mock_redis, sample_tokens)

    mock_redis.set.assert_called_once()
    args, kwargs = mock_redis.set.call_args
    assert args[0] == ss.TRADING_TOKEN_KEY
    assert ss._dec(args[1])["access_token"] == sample_tokens["access_token"]
    assert kwargs["ex"] == ss.TRADING_TOKEN_TTL_SECONDS

    mock_redis.get.return_value = args[1]
    assert ss.load_trading_tokens(mock_redis)["access_token"] == sample_tokens["access_token"]

def test_auto_trader_preference(mock_redis):
    ss.set_auto_trader_enabled(mock_redis, True)
    mock_redis.set.assert_called_once_with(ss.AUTO_TRADER_KEY, "1")

    mock_redis.get.return_value = "1"
    assert ss.get_auto_trader_enabled(mock_redis) is True

    mock_redis.get.return_value = "0"
    assert ss.get_auto_trader_enabled(mock_redis) is False

def test_active_market_session(mock_redis):
    ss.set_active_market_session(mock_redis, "session123", 3600)
    mock_redis.set.assert_called_once_with(ss.ACTIVE_MARKET_SESSION_KEY, "session123", ex=3600)

    mock_redis.get.return_value = "session123"
    assert ss.load_active_market_session(mock_redis) == "session123"

    ss.clear_active_market_session(mock_redis, "session123")
    mock_redis.delete.assert_called_once_with(ss.ACTIVE_MARKET_SESSION_KEY)

def test_need_refresh_true():
    # Token expired 10 seconds ago
    tok = {"expires_at": int(time.time()) - 10}
    assert ss.need_refresh(tok) is True
    
    # Token expires in 30 seconds (less than TOKEN_EXP_SKEW of 60)
    tok = {"expires_at": int(time.time()) + 30}
    assert ss.need_refresh(tok) is True

def test_need_refresh_false():
    # Token expires in 90 seconds (more than TOKEN_EXP_SKEW of 60)
    tok = {"expires_at": int(time.time()) + 90}
    assert ss.need_refresh(tok) is False

    # Token with very long expiry
    tok = {"expires_at": int(time.time()) + 3600}
    assert ss.need_refresh(tok) is False
