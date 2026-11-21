# backend/test/test_auth_api.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import json
import time

# Adjust path for imports and mock dependencies BEFORE app import
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

mock_redis_instance = MagicMock()
mock_build_auth_url = MagicMock(return_value="http://mock_schwab_auth_url")
mock_exchange_code_for_tokens = MagicMock()
mock_refresh_access_token = MagicMock()
mock_save_tokens = MagicMock()
mock_load_tokens = MagicMock()

from main import app
from routers import auth as auth_router
from routers.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME, _now

client = TestClient(app, base_url="https://testserver")

@pytest.fixture(autouse=True)
def reset_mocks():
    mock_redis_instance.reset_mock()
    mock_redis_instance.get.return_value = "present"
    mock_exchange_code_for_tokens.reset_mock()
    mock_refresh_access_token.reset_mock()
    mock_save_tokens.reset_mock()
    mock_load_tokens.reset_mock()
    mock_build_auth_url.reset_mock()
    mock_build_auth_url.return_value = "http://mock_schwab_auth_url"
    mock_load_tokens.return_value = None
    client.cookies.clear()
    with (
        patch.object(auth_router, "r", mock_redis_instance),
        patch.object(auth_router, "build_auth_url", mock_build_auth_url),
        patch.object(auth_router, "exchange_code_for_tokens", mock_exchange_code_for_tokens),
        patch.object(auth_router, "refresh_access_token", mock_refresh_access_token),
        patch.object(auth_router, "save_tokens", mock_save_tokens),
        patch.object(auth_router, "load_tokens", mock_load_tokens),
    ):
        yield


def test_auth_login_redirect():
    response = client.get("/auth/login", follow_redirects=False)
    
    assert response.status_code == 302 # Check for redirect
    assert response.headers["location"] == "http://mock_schwab_auth_url"
    
    # Check that a state was created in Redis
    mock_redis_instance.setex.assert_called_once()
    name, _, value = mock_redis_instance.setex.call_args.args
    assert name.startswith("oauth:state:")
    assert json.loads(value)["next"] == "/dashboard" # Default next path


def test_auth_callback_success():
    state = "test_state"
    code = "test_code"
    
    # Mock Redis lookup for the state
    mock_redis_instance.get.return_value = json.dumps({"next": "/custom_path"})
    
    # Mock token exchange
    mock_exchange_code_for_tokens.return_value = {
        "access_token": "final_access_token",
        "expires_in": 1800
    }
    
    response = client.get(f"/auth/callback?code={code}&state={state}", follow_redirects=False)

    # Check redirects and cookies
    assert response.status_code == 302
    assert response.headers["location"].endswith("/custom_path")
    assert SESSION_COOKIE_NAME in response.cookies
    assert CSRF_COOKIE_NAME in response.cookies
    
    # Check that state was retrieved and deleted
    mock_redis_instance.get.assert_any_call(f"oauth:state:{state}")
    mock_redis_instance.delete.assert_called_with(f"oauth:state:{state}")
    
    # Check that tokens were exchanged and persisted
    mock_exchange_code_for_tokens.assert_called_once_with(code)
    sid = response.cookies.get(SESSION_COOKIE_NAME)
    assert sid
    mock_save_tokens.assert_called_once()
    _, saved_sid, saved_tokens = mock_save_tokens.call_args.args
    assert saved_sid == sid
    assert saved_tokens["access_token"] == "final_access_token"
    assert saved_tokens["refresh_expires_at"] - saved_tokens["authorized_at"] == auth_router.SCHWAB_REFRESH_TOKEN_LIFETIME_SECONDS


def test_auth_callback_invalid_state():
    mock_redis_instance.get.return_value = None # State doesn't exist
    response = client.get("/auth/callback?code=any_code&state=invalid_state")
    assert response.status_code == 400
    assert "Invalid/expired state" in response.text


def test_auth_status_authenticated():
    session_id = "test_session_id"
    token_data = {
        "access_token": "valid_token",
        "expires_at": _now() + 1000
    }
    mock_load_tokens.return_value = token_data
    
    response = client.get("/auth/status", cookies={SESSION_COOKIE_NAME: session_id})
    
    assert response.status_code == 200
    data = response.json()
    assert data["authenticated"] is True
    assert data["refreshed"] is False
    assert data["seconds_to_expiry"] > 900
    mock_load_tokens.assert_called_with(mock_redis_instance, session_id)
    cookie_headers = response.headers.get_list("set-cookie")
    assert any(f"Max-Age={auth_router.SESSION_TTL_SECONDS}" in value for value in cookie_headers)


def test_auth_status_unauthenticated():
    mock_load_tokens.return_value = None
    response = client.get("/auth/status", cookies={SESSION_COOKIE_NAME: "invalid_session"})
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_auth_status_warns_before_schwab_reauthorization_deadline():
    session_id = "reauth_soon_session"
    mock_load_tokens.return_value = {
        "access_token": "valid_token",
        "expires_at": _now() + 1000,
        "authorized_at": _now() - (6 * 24 * 60 * 60),
        "refresh_expires_at": _now() + 3600,
    }

    response = client.get("/auth/status", cookies={SESSION_COOKIE_NAME: session_id})

    assert response.status_code == 200
    assert response.json()["reauthorization_soon"] is True
    assert response.json()["reauthorization_required"] is False
    assert 3500 <= response.json()["seconds_to_reauthorization"] <= 3600


def test_auth_status_refreshes_expiring_token():
    session_id = "expiring_session"
    mock_load_tokens.return_value = {
        "access_token": "old_token",
        "refresh_token": "refresh_token",
        "expires_at": _now() + 30,
    }
    mock_refresh_access_token.return_value = {
        "access_token": "new_token",
        "refresh_token": "refresh_token",
        "expires_in": 1800,
    }

    response = client.get("/auth/status", cookies={SESSION_COOKIE_NAME: session_id})

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["refreshed"] is True
    assert response.json()["seconds_to_expiry"] > 1700
    mock_refresh_access_token.assert_called_once_with("refresh_token")
    mock_save_tokens.assert_called_once()


def test_auth_status_handles_refresh_failure_without_server_error():
    mock_load_tokens.return_value = {
        "access_token": "expired_token",
        "refresh_token": "invalid_refresh_token",
        "expires_at": _now() - 10,
    }
    mock_refresh_access_token.side_effect = RuntimeError("Schwab rejected refresh")

    response = client.get("/auth/status", cookies={SESSION_COOKIE_NAME: "expired_session"})

    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "refresh_failed": True}


def test_auth_logout():
    session_id = "session_to_delete"
    csrf_token = "csrf_token"
    mock_redis_instance.get.return_value = csrf_token
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: csrf_token})
    
    assert response.status_code == 200
    assert response.json()["ok"] is True
    
    # Check that Redis keys were deleted
    assert mock_redis_instance.delete.call_count == 4
    mock_redis_instance.delete.assert_any_call(f"tok:{session_id}")
    mock_redis_instance.delete.assert_any_call(f"sess:{session_id}")
    mock_redis_instance.delete.assert_any_call(f"csrf:{session_id}")
    mock_redis_instance.delete.assert_any_call("trading:tokens")
    
    # Check for cookie deletion header
    assert "set-cookie" in response.headers
    assert f'{SESSION_COOKIE_NAME}=""' in response.headers["set-cookie"]
    assert f'{CSRF_COOKIE_NAME}=""' in response.headers["set-cookie"]
    assert "expires=" in response.headers["set-cookie"]


def test_auth_logout_rejects_missing_csrf():
    session_id = "session_to_delete"
    mock_redis_instance.get.return_value = "csrf_token"

    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    response = client.post("/auth/logout")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid CSRF token"


def test_auth_logout_retains_only_isolated_credentials_when_auto_trader_is_on():
    session_id = "session_with_auto_trader"
    csrf_token = "csrf_token"
    mock_redis_instance.get.return_value = csrf_token
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)

    with patch.object(auth_router, "get_auto_trader_enabled", return_value=True):
        response = client.post("/auth/logout", headers={CSRF_HEADER_NAME: csrf_token})

    assert response.status_code == 200
    assert response.json()["trading_credentials_retained"] is True
    mock_redis_instance.delete.assert_any_call(f"tok:{session_id}")
    mock_redis_instance.delete.assert_any_call(f"sess:{session_id}")
    mock_redis_instance.delete.assert_any_call(f"csrf:{session_id}")
    assert not any(call.args == ("trading:tokens",) for call in mock_redis_instance.delete.call_args_list)


def test_scanner_uses_isolated_auto_trader_credentials_after_ui_logout():
    with (
        patch.object(auth_router, "load_active_market_session", return_value=None),
        patch.object(auth_router, "get_auto_trader_enabled", return_value=True),
        patch.object(auth_router, "get_trading_access_token", return_value="worker_access_token") as worker_token,
    ):
        assert auth_router.get_market_data_access_token() == "worker_access_token"

    worker_token.assert_called_once_with()


def test_scanner_has_no_schwab_access_after_logout_when_auto_trader_is_off():
    with (
        patch.object(auth_router, "load_active_market_session", return_value=None),
        patch.object(auth_router, "get_auto_trader_enabled", return_value=False),
        pytest.raises(RuntimeError, match="No authenticated Schwab session"),
    ):
        auth_router.get_market_data_access_token()


def test_trading_authorization_status_does_not_call_schwab():
    now = _now()
    tokens = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": now + 1200,
        "refresh_expires_at": now + 86_400,
    }
    with (
        patch.object(auth_router, "get_auto_trader_enabled", return_value=True),
        patch.object(auth_router, "load_trading_tokens", return_value=tokens),
        patch.object(auth_router, "refresh_access_token") as refresh,
    ):
        status = auth_router.get_trading_authorization_status()

    assert status["has_trading_credentials"] is True
    assert status["has_refresh_token"] is True
    assert status["reauthorization_required"] is False
    assert status["seconds_to_reauthorization"] > 86_300
    refresh.assert_not_called()


def test_trading_refresh_failure_preserves_retained_credentials():
    tokens = {
        "access_token": "expired",
        "refresh_token": "refresh",
        "expires_at": _now() - 1,
        "authorized_at": _now() - 3600,
        "refresh_expires_at": _now() + 86_400,
    }
    with (
        patch.object(auth_router, "get_auto_trader_enabled", return_value=True),
        patch.object(auth_router, "load_trading_tokens", return_value=tokens),
        patch.object(
            auth_router,
            "refresh_access_token",
            side_effect=RuntimeError("temporary network outage"),
        ),
        patch.object(auth_router, "delete_trading_tokens") as delete,
        pytest.raises(RuntimeError, match="temporary network outage"),
    ):
        auth_router.get_trading_access_token()

    delete.assert_not_called()


def test_auth_disconnect_removes_all_credentials_and_disables_auto_trader():
    session_id = "session_to_disconnect"
    csrf_token = "csrf_token"
    mock_redis_instance.get.return_value = csrf_token
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)

    response = client.post("/auth/disconnect", headers={CSRF_HEADER_NAME: csrf_token})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "auto_trader_enabled": False,
        "trading_credentials_retained": False,
        "provider_access_revoked": False,
    }
    mock_redis_instance.delete.assert_any_call(f"tok:{session_id}")
    mock_redis_instance.delete.assert_any_call(f"sess:{session_id}")
    mock_redis_instance.delete.assert_any_call(f"csrf:{session_id}")
    mock_redis_instance.delete.assert_any_call("trading:tokens")
    mock_redis_instance.set.assert_called_with("trading:auto_enabled", "0")
    assert f'{SESSION_COOKIE_NAME}=""' in response.headers["set-cookie"]
