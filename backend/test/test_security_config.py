import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def test_cors_origins_use_explicit_env(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com, http://localhost:3000 ")

    sys.modules.pop("main", None)
    import main

    assert main._cors_origins() == ["https://app.example.com", "http://localhost:3000"]


def test_cors_origins_include_frontend_url_when_no_explicit_env(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://stock.example.com/")

    sys.modules.pop("main", None)
    import main

    assert "https://stock.example.com" in main._cors_origins()


def test_auth_cookie_rejects_samesite_none_without_secure(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "None")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    sys.modules.pop("routers.auth", None)
    with patch("redis.from_url", return_value=MagicMock()):
        with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE must be true"):
            importlib.import_module("routers.auth")


def test_auth_cookie_rejects_invalid_samesite(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "Invalid")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")

    sys.modules.pop("routers.auth", None)
    with patch("redis.from_url", return_value=MagicMock()):
        with pytest.raises(RuntimeError, match="SESSION_COOKIE_SAMESITE"):
            importlib.import_module("routers.auth")


def teardown_module():
    os.environ.pop("SESSION_COOKIE_SAMESITE", None)
    os.environ.pop("SESSION_COOKIE_SECURE", None)
    sys.modules.pop("routers.auth", None)
