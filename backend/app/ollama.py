from typing import Any

import httpx

from .config import settings


class OllamaError(Exception):
    pass


class OllamaUnavailable(OllamaError):
    pass


class OllamaTimeout(OllamaError):
    pass


async def get_status(model: str | None = None) -> dict[str, Any]:
    selected_model = model or settings.ollama_model
    if not settings.ollama_url_is_local:
        return {
            "available": False,
            "model": selected_model,
            "model_available": False,
            "error": "Remote Ollama URLs are disabled. Configure a localhost URL.",
        }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException:
        return {
            "available": False,
            "model": selected_model,
            "model_available": False,
            "timed_out": True,
            "error": "Ollama did not respond before the connection timeout.",
        }
    except (httpx.HTTPError, ValueError):
        return {
            "available": False,
            "model": selected_model,
            "model_available": False,
            "timed_out": False,
            "error": "Cannot reach Ollama at the configured localhost URL.",
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("models", []), list):
        return {
            "available": False,
            "model": selected_model,
            "model_available": False,
            "timed_out": False,
            "error": "Ollama returned an invalid models response.",
        }
    models = payload.get("models", [])
    installed = any(
        isinstance(item, dict)
        and (item.get("name") == selected_model or item.get("model") == selected_model)
        for item in models
    )
    return {
        "available": True,
        "model": selected_model,
        "model_available": installed,
        "timed_out": False,
        "error": None if installed else f"Model is not installed. Run: ollama pull {selected_model}",
    }


async def list_models() -> dict[str, Any]:
    status = await get_status()
    if not status["available"]:
        if status.get("timed_out"):
            raise OllamaTimeout(status["error"] or "Ollama timed out.")
        return {"configured_model": settings.ollama_model, "available": False, "models": [], "error": status["error"]}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise OllamaTimeout("Ollama did not respond before the connection timeout.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OllamaUnavailable("Ollama returned an invalid models response.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models", []), list):
        raise OllamaUnavailable("Ollama returned an invalid models response.")
    return {
        "configured_model": settings.ollama_model,
        "available": True,
        "models": [
            item.get("name", item.get("model", ""))
            for item in payload.get("models", [])
            if isinstance(item, dict)
        ],
        "error": None,
    }


async def chat(messages: list[dict[str, str]], model: str | None = None) -> str:
    selected_model = model or settings.ollama_model
    status = await get_status(selected_model)
    if not status["available"]:
        if not settings.ollama_url_is_local:
            raise OllamaUnavailable(status["error"] or "Remote Ollama URLs are disabled.")
        if status.get("timed_out"):
            raise OllamaTimeout(status["error"] or "Ollama timed out.")
        raise OllamaUnavailable(
            f"Ollama is unavailable at {settings.ollama_base_url}. Start Ollama and retry."
        )
    if not status["model_available"]:
        raise OllamaUnavailable(status["error"] or "Configured Ollama model is unavailable.")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/chat",
                json={"model": selected_model, "messages": messages, "stream": False},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise OllamaTimeout("Ollama did not respond before the chat timeout.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OllamaError("Ollama failed to complete the chat request.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        raise OllamaError("Ollama returned an invalid response.")
    answer = payload.get("message", {}).get("content")
    if not isinstance(answer, str) or not answer.strip():
        raise OllamaError("Ollama returned an empty or invalid response.")
    return answer.strip()
