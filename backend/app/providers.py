from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from . import ollama
from .config import settings


class ProviderError(Exception):
    status_code = 502


class ProviderNotConfigured(ProviderError):
    status_code = 503


class ProviderAuthenticationError(ProviderError):
    status_code = 502


class ProviderPermissionError(ProviderError):
    status_code = 502


class ProviderBillingError(ProviderError):
    status_code = 502


class ProviderUnavailable(ProviderError):
    status_code = 502


class ProviderTimeout(ProviderError):
    status_code = 504


class ProviderRateLimited(ProviderError):
    status_code = 429


class ProviderInvalidResponse(ProviderError):
    status_code = 502


class UnsupportedProvider(ProviderError):
    status_code = 422


class InvalidModel(ProviderError):
    status_code = 422


class ProviderInvalidRequest(ProviderError):
    status_code = 422


@dataclass(frozen=True)
class ModelReply:
    provider: str
    model: str
    content: str


class ModelProvider(Protocol):
    provider_id: str
    display_name: str
    cloud: bool

    def configured(self) -> tuple[bool, str | None]: ...

    def configured_model(self) -> str: ...

    async def models(self) -> list[str]: ...

    async def complete(self, messages: list[dict[str, str]], model: str) -> ModelReply: ...


async def _post(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: httpx.Timeout,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise ProviderTimeout("The model provider timed out. Try again shortly.") from exc
    except httpx.HTTPError as exc:
        raise ProviderUnavailable("Could not connect to the configured model provider.") from exc

    if response.status_code in {401, 403}:
        raise ProviderAuthenticationError(
            "The provider rejected its credentials. Check the backend API key."
        )
    if response.status_code == 429:
        raise ProviderRateLimited("The provider rate limit was reached. Wait and try again.")
    if response.is_error:
        if response.status_code in {400, 404, 422}:
            raise InvalidModel(
                f"The provider rejected the request (HTTP {response.status_code}). "
                "Check the configured model ID and provider request limits."
            )
        raise ProviderUnavailable(
            f"The provider could not complete the request (HTTP {response.status_code})."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderInvalidResponse("The model provider returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ProviderInvalidResponse("The model provider returned an invalid response.")
    return payload


def _extract_text(payload: dict[str, Any], provider: str) -> str:
    if provider == "openai":
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                text = message["content"].strip()
                if text:
                    return text
    elif provider == "anthropic":
        blocks = payload.get("content")
        if isinstance(blocks, list):
            text = "\n".join(
                block["text"]
                for block in blocks
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ).strip()
            if text:
                return text
    raise ProviderInvalidResponse(f"{provider} returned no text content.")


class OllamaProvider:
    provider_id = "ollama"
    display_name = "Ollama"
    cloud = False

    def configured(self) -> tuple[bool, str | None]:
        if not settings.ollama_url_is_local:
            return False, "OLLAMA_BASE_URL must use localhost or a loopback address."
        return True, None

    def configured_model(self) -> str:
        return settings.ollama_model

    async def models(self) -> list[str]:
        try:
            available = await ollama.list_models()
        except ollama.OllamaTimeout as exc:
            raise ProviderTimeout(str(exc)) from exc
        except ollama.OllamaError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        if not available["available"]:
            raise ProviderUnavailable(available.get("error") or "Ollama is unavailable.")
        return available["models"]

    async def complete(self, messages: list[dict[str, str]], model: str) -> ModelReply:
        try:
            content = await ollama.chat(messages, model=model)
        except ollama.OllamaTimeout as exc:
            raise ProviderTimeout(str(exc)) from exc
        except ollama.OllamaUnavailable as exc:
            raise ProviderNotConfigured(str(exc)) from exc
        except ollama.OllamaError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        return ModelReply(self.provider_id, model, content)


class OpenAIProvider:
    provider_id = "openai"
    display_name = "OpenAI"
    cloud = True

    def configured(self) -> tuple[bool, str | None]:
        if not settings.openai_api_key.strip():
            return False, "Set OPENAI_API_KEY in backend/.env."
        if not settings.openai_model.strip():
            return False, "Set OPENAI_MODEL in backend/.env."
        return True, None

    def configured_model(self) -> str:
        return settings.openai_model.strip()

    async def models(self) -> list[str]:
        configured, _ = self.configured()
        return [self.configured_model()] if configured else []

    async def complete(self, messages: list[dict[str, str]], model: str) -> ModelReply:
        configured, error = self.configured()
        if not configured:
            raise ProviderNotConfigured(error or "OpenAI is not configured.")
        payload = await _post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key.strip()}"},
            body={"model": model, "messages": messages},
            timeout=httpx.Timeout(90.0, connect=10.0),
        )
        return ModelReply(self.provider_id, model, _extract_text(payload, self.provider_id))


class AnthropicProvider:
    provider_id = "anthropic"
    display_name = "Anthropic Claude"
    cloud = True

    def configured(self) -> tuple[bool, str | None]:
        if not settings.anthropic_api_key.strip():
            return False, "Set ANTHROPIC_API_KEY in backend/.env."
        if not settings.anthropic_model.strip():
            return False, "Set ANTHROPIC_MODEL in backend/.env."
        return True, None

    def configured_model(self) -> str:
        return settings.anthropic_model.strip()

    async def models(self) -> list[str]:
        configured, _ = self.configured()
        return [self.configured_model()] if configured else []

    async def complete(self, messages: list[dict[str, str]], model: str) -> ModelReply:
        configured, error = self.configured()
        if not configured:
            raise ProviderNotConfigured(error or "Anthropic is not configured.")
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        conversation = [
            {"role": item["role"], "content": item["content"]}
            for item in messages
            if item["role"] in {"user", "assistant"}
        ]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 2048,
            "messages": conversation,
        }
        if system:
            body["system"] = system
        payload = await _post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key.strip(),
                "anthropic-version": "2023-06-01",
            },
            body=body,
            timeout=httpx.Timeout(90.0, connect=10.0),
        )
        return ModelReply(self.provider_id, model, _extract_text(payload, self.provider_id))


class GeminiProvider:
    provider_id = "gemini"
    display_name = "Google Gemini"
    cloud = True

    def configured(self) -> tuple[bool, str | None]:
        if not settings.gemini_api_key.strip():
            return False, "Set GEMINI_API_KEY in backend/.env."
        if not settings.gemini_model.strip():
            return False, "Set GEMINI_MODEL in backend/.env."
        if any(character.isspace() for character in settings.gemini_model.strip()):
            return False, (
                "GEMINI_MODEL must be a supported Gemini model ID without spaces, "
                "for example gemini-2.5-flash."
            )
        return True, None

    def configured_model(self) -> str:
        return settings.gemini_model.strip()

    async def models(self) -> list[str]:
        configured, _ = self.configured()
        return [self.configured_model()] if configured else []

    async def complete(self, messages: list[dict[str, str]], model: str) -> ModelReply:
        configured, error = self.configured()
        if not configured:
            raise ProviderNotConfigured(error or "Google Gemini is not configured.")
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        contents = [
            genai_types.Content(
                role="model" if item["role"] == "assistant" else "user",
                parts=[genai_types.Part.from_text(text=item["content"])],
            )
            for item in messages
            if item["role"] in {"user", "assistant"}
        ]
        client = genai.Client(
            api_key=settings.gemini_api_key.strip(),
            http_options=genai_types.HttpOptions(timeout=90_000),
        )
        async_client = client.aio
        try:
            response = await async_client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system or None,
                ),
            )
        except genai_errors.APIError as exc:
            error_detail = str(exc).casefold()
            if exc.code == 401 or (
                exc.code == 400
                and ("api key" in error_detail or "api_key" in error_detail)
            ):
                raise ProviderAuthenticationError(
                    "Gemini rejected the configured API key. Check GEMINI_API_KEY."
                ) from None
            if exc.code == 403:
                raise ProviderPermissionError(
                    "Gemini denied access to this API or model. Check the API key's project access and billing."
                ) from None
            if exc.code == 402:
                raise ProviderBillingError(
                    "Gemini billing is unavailable or the account has insufficient credit. Check billing settings."
                ) from None
            if exc.code == 429:
                raise ProviderRateLimited(
                    "Gemini rate limit or quota was reached. Wait for quota to reset or review the API project's limits."
                ) from None
            if exc.code in {408, 504}:
                raise ProviderTimeout("Gemini timed out. Try again shortly.") from None
            if exc.code == 404:
                raise InvalidModel(
                    "Gemini does not recognize this model. Check GEMINI_MODEL against the models available to your API key."
                ) from None
            if exc.code in {400, 422}:
                raise ProviderInvalidRequest(
                    "Gemini rejected the request format or parameters. Check the message and supported request options."
                ) from None
            raise ProviderUnavailable(
                f"Gemini could not complete the request (HTTP {exc.code})."
            ) from None
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderTimeout("Gemini timed out. Try again shortly.") from None
        except httpx.HTTPError:
            raise ProviderUnavailable(
                "Could not connect to Gemini. Check the network and try again."
            ) from None
        finally:
            try:
                await async_client.aclose()
            finally:
                client.close()
        try:
            text = getattr(response, "text", None)
        except ValueError as exc:
            raise ProviderInvalidResponse(
                "Gemini returned no text. The prompt may have been blocked or the response was empty."
            ) from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderInvalidResponse(
                "Gemini returned no text. The prompt may have been blocked or the response was empty."
            )
        return ModelReply(self.provider_id, model, text.strip())


PROVIDER_TYPES: tuple[type[ModelProvider], ...] = (
    OllamaProvider,
    OpenAIProvider,
    AnthropicProvider,
    GeminiProvider,
)


def provider_registry() -> dict[str, ModelProvider]:
    return {provider.provider_id: provider() for provider in PROVIDER_TYPES}


def list_provider_status() -> dict[str, Any]:
    registry = provider_registry()
    entries = []
    for provider_id, provider in registry.items():
        configured, error = provider.configured()
        entries.append(
            {
                "id": provider_id,
                "name": provider.display_name,
                "cloud": provider.cloud,
                "configured": configured,
                "selected": provider_id == settings.model_provider,
                "model": provider.configured_model() if configured else None,
                "configuration_error": error,
                "connection_verified": False,
                "privacy": provider_privacy(provider_id),
            }
        )
    return {"selected_provider": settings.model_provider, "providers": entries}


PROVIDER_PRIVACY: dict[str, dict[str, Any]] = {
    "ollama": {
        "processing": "local",
        "api_key_required": False,
        "leaves_device": False,
        "data_sent": (
            "Your message, eligible conversation history, and any document passages included "
            "with your request. Inference runs on the Ollama server on this device."
        ),
    },
    "openai": {
        "processing": "external",
        "api_key_required": True,
        "leaves_device": True,
        "data_sent": (
            "Your message and eligible conversation history. Document excerpts are sent only "
            "after you confirm the exact excerpts in AI Chat."
        ),
    },
    "anthropic": {
        "processing": "external",
        "api_key_required": True,
        "leaves_device": True,
        "data_sent": (
            "Your message and eligible conversation history. Document excerpts are sent only "
            "after you confirm the exact excerpts in AI Chat."
        ),
    },
    "gemini": {
        "processing": "external",
        "api_key_required": True,
        "leaves_device": True,
        "data_sent": (
            "Your message and eligible conversation history. Document excerpts are sent only "
            "after you confirm the exact excerpts in AI Chat."
        ),
    },
}


def provider_privacy(provider_id: str) -> dict[str, Any]:
    return PROVIDER_PRIVACY.get(
        provider_id,
        {
            "processing": "unknown",
            "api_key_required": True,
            "leaves_device": True,
            "data_sent": "Provider behaviour is not documented for this integration.",
        },
    )


def select_provider(provider_id: str | None = None) -> ModelProvider:
    selected = (provider_id or settings.model_provider).strip().lower()
    if selected == "google":
        selected = "gemini"
    provider = provider_registry().get(selected)
    if provider is None:
        supported = ", ".join(provider_registry())
        raise UnsupportedProvider(f"Unsupported provider '{selected}'. Choose one of: {supported}.")
    configured, error = provider.configured()
    if not configured:
        raise ProviderNotConfigured(error or f"{provider.display_name} is not configured.")
    return provider


async def list_models(provider_id: str | None = None) -> dict[str, Any]:
    provider = select_provider(provider_id)
    models = await provider.models()
    return {
        "provider": provider.provider_id,
        "configured_model": provider.configured_model(),
        "models": models,
        "connection_verified": provider.provider_id == "ollama",
    }


async def complete(
    messages: list[dict[str, str]],
    provider_id: str | None = None,
    model: str | None = None,
) -> ModelReply:
    provider = select_provider(provider_id)
    selected_model = (model or provider.configured_model()).strip()
    if not selected_model:
        raise ProviderNotConfigured(f"Configure a model ID for {provider.display_name}.")
    if provider.cloud and selected_model != provider.configured_model():
        raise InvalidModel(
            "The requested model is not the configured model for this provider. "
            "Update its backend environment variable and restart the API."
        )
    if provider.provider_id == "ollama":
        installed_models = await provider.models()
        if selected_model not in installed_models:
            raise InvalidModel(
                f"Model '{selected_model}' is not installed locally. "
                "Install it with ollama pull <model> or choose an installed model."
            )
    return await provider.complete(messages, selected_model)
