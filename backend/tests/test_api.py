from io import BytesIO

import pytest
from pypdf import PdfWriter

from app import ollama, providers


def test_health_and_ollama_unavailable(client, monkeypatch):
    async def unavailable():
        return {"available": False, "model": "qwen2.5:3b", "model_available": False, "error": "offline"}

    monkeypatch.setattr(ollama, "get_status", unavailable)
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/health/ollama").json()["available"] is False


def test_model_provider_status_and_cors_origins(client, monkeypatch):
    from app import config

    response = client.get(
        "/api/health/model",
        headers={"Origin": "http://127.0.0.1:3000"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert response.json()["configured"] is True
    assert response.json()["connection_verified"] is False

    denied = client.get(
        "/api/health/model",
        headers={"Origin": "https://example.com"},
    )
    assert "access-control-allow-origin" not in denied.headers

    monkeypatch.setattr(config.settings, "model_provider", "future-cloud-provider")
    response = client.get("/api/health/model")
    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert "Unsupported provider" in response.json()["error"]


def test_settings_reject_non_loopback_bind_and_dashboard_origins():
    from app.config import Settings

    for host in ("0.0.0.0", "192.0.2.10", "example.com"):
        with pytest.raises(ValueError, match="APP_HOST"):
            Settings(host=host)

    for origin in (
        "https://example.com",
        "http://localhost:3000/dashboard",
        "http://user@localhost:3000",
    ):
        with pytest.raises(ValueError, match="FRONTEND_ORIGIN"):
            Settings(frontend_origin=origin)

    assert Settings(host="::1", frontend_origin="http://[::1]:3000").host == "::1"


def test_provider_inventory_and_cloud_models_are_configuration_only(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "model_provider", "ollama")
    monkeypatch.setattr(config.settings, "openai_api_key", "test-secret-key")
    monkeypatch.setattr(config.settings, "openai_model", "test-model")
    inventory = client.get("/api/providers")
    assert inventory.status_code == 200
    data = inventory.json()
    openai = next(item for item in data["providers"] if item["id"] == "openai")
    assert openai["configured"] is True
    assert openai["connection_verified"] is False
    assert "test-secret-key" not in inventory.text
    model_list = client.get("/api/models?provider=openai")
    assert model_list.status_code == 200
    assert model_list.json() == {
        "provider": "openai",
        "configured_model": "test-model",
        "models": ["test-model"],
        "connection_verified": False,
    }


def test_gemini_missing_credentials_returns_configuration_error(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "gemini_api_key", "")
    monkeypatch.setattr(config.settings, "gemini_model", "gemini-2.5-flash")
    response = client.post(
        "/api/chat",
        json={"message": "Hello", "provider": "gemini"},
    )

    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_gemini_chat_uses_sdk_history_and_persists_provider_metadata(client, monkeypatch):
    from types import SimpleNamespace

    from app import config

    monkeypatch.setattr(config.settings, "gemini_api_key", "test-gemini-secret")
    monkeypatch.setattr(config.settings, "gemini_model", "gemini-2.5-flash")
    client.post(
        "/api/documents/upload",
        files={
            "file": (
                "private.txt",
                b"The private project passphrase is cedar-551.",
                "text/plain",
            )
        },
    )

    class FakeClient:
        def __init__(self):
            self.aio = self
            self.models = self
            self.calls = []

        async def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(text="A genuine response from the mocked SDK.")

        async def aclose(self):
            return None

        def close(self):
            return None
    fake_client = FakeClient()
    monkeypatch.setattr(providers.genai, "Client", lambda **_kwargs: fake_client)
    first = client.post(
        "/api/chat",
        json={"message": "Remember the word cedar.", "provider": "gemini"},
    )
    assert first.status_code == 200
    assert client.post("/api/documents/search", json={"query": "cedar"}).json()
    assert first.json()["document_context_included"] is False
    assert first.json()["message"]["sources"] == []
    assert all(
        "cedar-551" not in part.text
        for item in fake_client.calls[0]["contents"]
        for part in item.parts
    )
    conversation_id = first.json()["conversation_id"]
    second = client.post(
        "/api/chat",
        json={
            "message": "What word did I ask you to remember?",
            "provider": "gemini",
            "conversation_id": conversation_id,
        },
    )

    assert second.status_code == 200
    result = second.json()
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-2.5-flash"
    assert result["message"]["provider"] == "gemini"
    assert result["message"]["model"] == "gemini-2.5-flash"
    assert result["message"]["content"] == "A genuine response from the mocked SDK."
    assert result["document_context_included"] is False
    assert result["message"]["sources"] == []
    assert [(item.role, item.parts[0].text) for item in fake_client.calls[1]["contents"]] == [
        ("user", "Remember the word cedar."),
        ("model", "A genuine response from the mocked SDK."),
        ("user", "What word did I ask you to remember?"),
    ]
    assert all(
        "cedar-551" not in part.text
        for item in fake_client.calls[1]["contents"]
        for part in item.parts
    )
    assert fake_client.calls[1]["config"].system_instruction is not None
    history = client.get(f"/api/conversations/{conversation_id}/messages").json()
    assert [item["provider"] for item in history] == [
        "gemini",
        "gemini",
        "gemini",
        "gemini",
    ]


def test_gemini_chat_error_does_not_fall_back_or_expose_provider_body(client, monkeypatch):
    from app import config
    from app.providers import genai_errors

    monkeypatch.setattr(config.settings, "gemini_api_key", "test-gemini-secret")
    monkeypatch.setattr(config.settings, "gemini_model", "gemini-2.5-flash")

    class FailedClient:
        def __init__(self):
            self.aio = self
            self.models = self

        async def generate_content(self, **_kwargs):
            raise genai_errors.APIError(
                401,
                {"error": {"message": "provider echoed sensitive data"}},
            )

        async def aclose(self):
            return None

        def close(self):
            return None
    monkeypatch.setattr(providers.genai, "Client", lambda **_kwargs: FailedClient())
    response = client.post(
        "/api/chat",
        json={"message": "Hello", "provider": "gemini"},
    )

    assert response.status_code == 502
    assert "GEMINI_API_KEY" in response.json()["detail"]
    assert "provider echoed sensitive data" not in response.text


def test_ollama_model_inventory_timeout_is_reported_as_gateway_timeout(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "model_provider", "ollama")

    async def timed_out():
        return {
            "available": False,
            "model": "qwen2.5:3b",
            "model_available": False,
            "timed_out": True,
            "error": "Ollama did not respond before the connection timeout.",
        }

    monkeypatch.setattr(ollama, "get_status", timed_out)
    response = client.get("/api/models")
    assert response.status_code == 504
    assert "timeout" in response.json()["detail"].casefold()


def test_chat_validation_and_missing_conversation(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    response = client.post("/api/chat", json={"message": "hello", "conversation_id": "missing"})
    assert response.status_code == 404


def test_document_upload_validation_and_size_limit(client):
    unsupported = client.post(
        "/api/documents/upload",
        files={"file": ("sample.exe", b"data", "application/octet-stream")},
    )
    assert unsupported.status_code == 415
    oversized = client.post(
        "/api/documents/upload",
        files={"file": ("large.txt", b"a" * 1025, "text/plain")},
    )
    assert oversized.status_code == 413
    malformed_pdf = client.post(
        "/api/documents/upload",
        files={"file": ("broken.pdf", b"%PDF-not-a-pdf", "application/pdf")},
    )
    assert malformed_pdf.status_code == 400


@pytest.mark.parametrize("filename", ["notes.txt", "notes.md"])
def test_text_document_ingestion_listing_and_search(client, filename):
    response = client.post(
        "/api/documents/upload",
        files={"file": (filename, b"Local privacy-first assistant notes.", "text/plain")},
    )
    assert response.status_code == 201
    assert response.json()["original_name"] == filename
    listed = client.get("/api/documents").json()
    assert len(listed) == 1
    matches = client.post("/api/documents/search", json={"query": "privacy assistant"}).json()
    assert len(matches) == 1
    assert matches[0]["filename"] == filename


def test_pdf_page_references(client):
    output = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference}),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (Project Aurora launch notes) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    response = client.post(
        "/api/documents/upload",
        files={"file": ("notes.pdf", output.getvalue(), "application/pdf")},
    )
    assert response.status_code == 201
    matches = client.post("/api/documents/search", json={"query": "Aurora"}).json()
    assert matches and matches[0]["page_number"] == 1


def test_chat_persistence_and_ollama_failures(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "ollama_model", "qwen2.5:3b")

    async def installed_models(self):
        return ["qwen2.5:3b"]

    async def unavailable(_messages, model=None):
        raise ollama.OllamaUnavailable("Ollama is offline.")

    monkeypatch.setattr(providers.OllamaProvider, "models", installed_models)
    monkeypatch.setattr(ollama, "chat", unavailable)
    failure = client.post("/api/chat", json={"message": "What is new?"})
    assert failure.status_code == 503
    conversations = client.get("/api/conversations").json()
    assert len(conversations) == 1
    conversation_id = conversations[0]["id"]
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()
    assert len(messages) == 1 and messages[0]["role"] == "user"

    async def broken(_messages, model=None):
        raise ollama.OllamaError("bad response")

    monkeypatch.setattr(ollama, "chat", broken)
    assert client.post("/api/chat", json={"message": "Try again"}).status_code == 502


def test_chat_success_returns_persisted_messages_and_source(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "ollama_model", "qwen2.5:3b")

    async def installed_models(self):
        return ["qwen2.5:3b"]

    async def answer(messages, model=None):
        assert messages[0]["role"] == "system"
        assert any("untrusted reference passages" in item["content"].casefold() for item in messages)
        return "The notes describe Project Aurora. [Source 1]"

    monkeypatch.setattr(providers.OllamaProvider, "models", installed_models)
    monkeypatch.setattr(ollama, "chat", answer)
    client.post(
        "/api/documents/upload",
        files={"file": ("notes.txt", b"Project Aurora launch notes.", "text/plain")},
    )
    response = client.post("/api/chat", json={"message": "What about Project Aurora?"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["user_message"]["role"] == "user"
    assert payload["message"]["role"] == "assistant"
    assert payload["message"]["sources"][0]["filename"] == "notes.txt"
    persisted = client.get(
        f"/api/conversations/{payload['conversation_id']}/messages"
    ).json()
    assert [item["role"] for item in persisted] == ["user", "assistant"]
    assert persisted[1]["provider"] == "ollama"
    assert persisted[1]["model"] == "qwen2.5:3b"


def test_cloud_context_requires_explicit_consent(client, monkeypatch):
    from app import config, database

    monkeypatch.setattr(config.settings, "openai_api_key", "test-secret-key")
    monkeypatch.setattr(config.settings, "openai_model", "test-model")
    client.post(
        "/api/documents/upload",
        files={
            "file": (
                "private.txt",
                b"The Orion launch date is September 9. CURRENT-EXCERPT-ONLY.",
                "text/plain",
            )
        },
    )
    with database.session() as connection:
        conversation_id = database.create_conversation(connection, "Previous sourced exchange")
        database.add_message(
            connection,
            conversation_id,
            "user",
            "What is in my private archived note?",
            provider="ollama",
            model="qwen2.5:3b",
        )
        database.add_message(
            connection,
            conversation_id,
            "assistant",
            "The note contains PRIVATE-PAST-EXCERPT.",
            [{"document_id": "past", "snippet": "PRIVATE-PAST-EXCERPT"}],
            provider="ollama",
            model="qwen2.5:3b",
        )
    requests = []

    async def complete(messages, provider_id=None, model=None):
        requests.append((messages, provider_id, model))
        return providers.ModelReply(provider_id, model, "General response without document context.")

    monkeypatch.setattr(providers, "complete", complete)
    endpoint = "/api/chat"
    payload = {
        "message": "What is the Orion launch date?",
        "provider": "openai",
        "conversation_id": conversation_id,
    }
    without_consent = client.post(endpoint, json=payload)
    assert without_consent.status_code == 200
    assert without_consent.json()["document_context_included"] is False
    assert without_consent.json()["message"]["sources"] == []
    assert all("September 9" not in item["content"] for item in requests[-1][0])
    assert all("PRIVATE-PAST-EXCERPT" not in item["content"] for item in requests[-1][0])

    missing_preview = client.post(
        endpoint,
        json={**payload, "allow_cloud_document_context": True},
    )
    assert missing_preview.status_code == 422

    preview = client.post(
        "/api/documents/preview",
        json={"message": payload["message"]},
    )
    assert preview.status_code == 200
    assert preview.json()[0]["filename"] == "private.txt"
    with_consent = client.post(
        endpoint,
        json={
            **payload,
            "allow_cloud_document_context": True,
            "document_context_preview": preview.json(),
        },
    )
    assert with_consent.status_code == 200
    assert with_consent.json()["document_context_included"] is True
    assert with_consent.json()["message"]["sources"][0]["filename"] == "private.txt"
    assert any("September 9" in item["content"] for item in requests[-1][0])
    assert any("CURRENT-EXCERPT-ONLY" in item["content"] for item in requests[-1][0])
    assert all("PRIVATE-PAST-EXCERPT" not in item["content"] for item in requests[-1][0])
    assert with_consent.json()["message"]["provider"] == "openai"
    assert with_consent.json()["message"]["model"] == "test-model"

    calls_before_stale_send = len(requests)
    client.delete(f"/api/documents/{preview.json()[0]['document_id']}")
    stale = client.post(
        endpoint,
        json={
            **payload,
            "allow_cloud_document_context": True,
            "document_context_preview": preview.json(),
        },
    )
    assert stale.status_code == 409
    assert len(requests) == calls_before_stale_send


def test_storage_is_application_scoped(client, tmp_path):
    client.post(
        "/api/documents/upload",
        files={"file": ("..\\..\\outside.txt", b"private local document", "text/plain")},
    )
    files = list((tmp_path / "data" / "documents").iterdir())
    assert len(files) == 1
    assert files[0].parent == (tmp_path / "data" / "documents")
    assert files[0].name != "outside.txt"
