from unittest.mock import MagicMock
from types import SimpleNamespace

from services import ollama_manager


def test_runtime_settings_default_and_update():
    client = MagicMock()
    client.hgetall.return_value = {}
    assert ollama_manager.get_settings(client) == {
        "enabled": False,
        "active_model": "",
    }

    client.hgetall.return_value = {"enabled": "0", "active_model": "qwen3:4b"}
    updated = ollama_manager.update_settings(client, enabled=False, active_model="qwen3:4b")

    client.hset.assert_called_once_with(
        ollama_manager.SETTINGS_KEY,
        mapping={"enabled": "0", "active_model": "qwen3:4b"},
    )
    assert updated["active_model"] == "qwen3:4b"


def test_model_name_rejects_shell_or_url_input():
    for value in (
        "qwen3;rm",
        "http://example.test/model",
        "model name",
        "example:cloud",
        "example:30b-cloud",
        "example:4b-mlx",
    ):
        try:
            ollama_manager.update_settings(MagicMock(), active_model=value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{value!r} should be rejected")


def test_server_status_reports_installed_and_running_models(monkeypatch):
    client = MagicMock()
    client.hgetall.return_value = {"enabled": "1", "active_model": "qwen3:1.7b"}
    responses = {
        "/api/version": {"version": "1.0"},
        "/api/tags": {"models": [{"name": "qwen3:1.7b"}]},
        "/api/ps": {"models": [{"name": "qwen3:1.7b"}]},
    }
    monkeypatch.setattr(
        ollama_manager,
        "_request",
        lambda _method, path, **_kwargs: responses[path],
    )

    status = ollama_manager.server_status(client)

    assert status["available"] is True
    assert status["active_model_installed"] is True
    assert status["running_models"][0]["name"] == "qwen3:1.7b"


def test_server_status_reports_actionable_offline_message(monkeypatch):
    client = MagicMock()
    client.hgetall.return_value = {}
    monkeypatch.setattr(
        ollama_manager,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ollama_manager.requests.ConnectionError("internal DNS details")
        ),
    )

    status = ollama_manager.server_status(client)

    assert status["available"] is False
    assert "ollama-news" in status["error"]
    assert "internal DNS details" not in status["error"]


def test_delete_unselected_models_keeps_only_selected(monkeypatch):
    monkeypatch.setattr(
        ollama_manager,
        "list_models",
        lambda: [{"name": "selected:latest"}, {"name": "old:1b"}, {"name": "old:3b"}],
    )
    removed = []
    monkeypatch.setattr(
        ollama_manager,
        "delete_model",
        lambda model: removed.append(model) or {"ok": True, "model": model},
    )

    result = ollama_manager.delete_unselected_models("selected:latest")

    assert result == ["old:1b", "old:3b"]
    assert removed == ["old:1b", "old:3b"]


def test_catalog_summary_exposes_registry_metadata():
    html = """
    <a href="/library/example">
      <div><h2>example</h2><p>Example model description.</p></div>
      <div class="flex flex-wrap"><span>tools</span><span>3b</span></div>
      <p><span>1.2M</span><span>Pulls</span><span>7</span><span>Tags</span>
      <span title="Jul 1, 2026 1:00 PM UTC">Updated 3 weeks ago</span></p>
    </a>
    """
    anchor = ollama_manager.BeautifulSoup(html, "html.parser").select_one("a")

    result = ollama_manager._catalog_summary(anchor)

    assert result["name"] == "example"
    assert result["description"] == "Example model description."
    assert result["capabilities"] == ["tools"]
    assert result["parameter_sizes"] == ["3b"]
    assert result["pulls"] == "1.2M"
    assert result["tag_count"] == 7
    assert result["updated_at"] == "Jul 1, 2026 1:00 PM UTC"


def test_request_exposes_ollama_error_body(monkeypatch):
    monkeypatch.setattr(
        ollama_manager.requests,
        "request",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            status_code=500,
            json=lambda: {"error": "model requires a newer Ollama version"},
            text="",
        ),
    )

    try:
        ollama_manager._request("POST", "/api/pull", json={"model": "example"})
    except RuntimeError as exc:
        assert "requires a newer Ollama version" in str(exc)
    else:
        raise AssertionError("Ollama error response should be raised")
