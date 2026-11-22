import asyncio

import httpx
import pytest

from app import ollama


def test_ollama_chat_uses_configured_local_model(monkeypatch):
    monkeypatch.setattr(ollama.settings, "ollama_base_url", "http://localhost:11434")
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": ollama.settings.ollama_model}]},
            )
        return httpx.Response(200, json={"message": {"content": "Local model reply"}})

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(ollama.httpx, "AsyncClient", client_factory)
    answer = asyncio.run(ollama.chat([{"role": "user", "content": "Hello"}]))
    assert answer == "Local model reply"


def test_ollama_invalid_model_list_is_reported(monkeypatch):
    monkeypatch.setattr(ollama.settings, "ollama_base_url", "http://localhost:11434")
    original_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(ollama.httpx, "AsyncClient", client_factory)
    result = asyncio.run(ollama.get_status())
    assert result["available"] is False
    assert result["error"] == "Ollama returned an invalid models response."


def test_ollama_reports_when_configured_model_is_not_installed(monkeypatch):
    monkeypatch.setattr(ollama.settings, "ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(ollama.settings, "ollama_model", "missing-model:latest")
    original_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "installed-model:latest"}]})

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(ollama.httpx, "AsyncClient", client_factory)
    result = asyncio.run(ollama.get_status())

    assert result["available"] is True
    assert result["model_available"] is False
    assert "ollama pull missing-model:latest" in result["error"]


def test_ollama_connection_failure_is_reported_without_raw_error(monkeypatch):
    monkeypatch.setattr(ollama.settings, "ollama_base_url", "http://localhost:11434")
    original_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sensitive connection details")

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(ollama.httpx, "AsyncClient", client_factory)
    result = asyncio.run(ollama.get_status())

    assert result["available"] is False
    assert "Cannot reach Ollama" in result["error"]
    assert "sensitive connection details" not in result["error"]


def test_remote_ollama_url_is_blocked(monkeypatch):
    monkeypatch.setattr(ollama.settings, "ollama_base_url", "https://example.com")
    result = asyncio.run(ollama.get_status())
    assert result["available"] is False
    assert result["model_available"] is False
    assert "Remote Ollama URLs are disabled" in result["error"]


def test_ollama_timeout_is_reported_as_timeout(monkeypatch):
    monkeypatch.setattr(ollama.settings, "ollama_base_url", "http://localhost:11434")
    original_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock timeout")

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(ollama.httpx, "AsyncClient", client_factory)
    with pytest.raises(ollama.OllamaTimeout, match="timeout"):
        asyncio.run(ollama.list_models())
