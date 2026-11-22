import logging
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .config import ResearchSettings
from .models import SearchResult
from .urls import UnsafeURL, validate_web_url

logger = logging.getLogger(__name__)
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class SearchProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[SearchResult]: ...


class TavilySearchProvider:
    def __init__(
        self,
        settings: ResearchSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        if not self._settings.tavily_api_key:
            raise SearchProviderError(
                "provider_not_configured",
                "Web search is not configured. Set TAVILY_API_KEY in the backend environment.",
            )

        payload = {
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            if self._client is not None:
                response = await self._client.post(
                    TAVILY_SEARCH_URL,
                    headers={"Authorization": "Bearer " + self._settings.tavily_api_key},
                    json=payload,
                    timeout=self._settings.search_timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.search_timeout_seconds
                ) as client:
                    response = await client.post(
                        TAVILY_SEARCH_URL,
                        headers={"Authorization": "Bearer " + self._settings.tavily_api_key},
                        json=payload,
                    )
        except httpx.TimeoutException as exc:
            raise SearchProviderError("search_timeout", "The web search provider timed out.") from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                "search_unavailable", "Could not connect to the web search provider."
            ) from exc

        if response.status_code == 429:
            raise SearchProviderError(
                "search_rate_limited", "The web search provider rate limit was reached."
            )
        if response.is_error:
            raise SearchProviderError(
                "search_provider_error",
                f"The web search provider returned HTTP {response.status_code}.",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise SearchProviderError(
                "malformed_search_response", "The web search provider returned invalid JSON."
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise SearchProviderError(
                "malformed_search_response",
                "The web search provider returned an invalid results structure.",
            )

        results: list[SearchResult] = []
        for item in data["results"][:limit]:
            result = self._parse_result(item)
            results.append(result)
        return results

    @staticmethod
    def _parse_result(item: object) -> SearchResult:
        if not isinstance(item, dict):
            raise SearchProviderError(
                "malformed_search_response", "The web search provider returned an invalid result."
            )
        url = item.get("url")
        title = item.get("title")
        snippet = item.get("content", "")
        published_at = item.get("published_date")
        score = item.get("score")
        if (
            not isinstance(url, str)
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(snippet, str)
            or (published_at is not None and not isinstance(published_at, str))
            or (
                score is not None
                and (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not 0 <= score <= 1
                )
            )
        ):
            raise SearchProviderError(
                "malformed_search_response", "The web search provider returned an invalid result."
            )

        try:
            validate_web_url(url)
        except UnsafeURL as exc:
            logger.info("Skipping invalid URL in search-provider response.")
            raise SearchProviderError(
                "malformed_search_response",
                "The web search provider returned an invalid source URL.",
            ) from exc

        domain = urlsplit(url).hostname
        try:
            return SearchResult(
                title=title.strip(),
                url=url,
                snippet=snippet,
                published_at=published_at,
                domain=domain,
                score=score,
            )
        except ValidationError as exc:
            raise SearchProviderError(
                "malformed_search_response",
                "The web search provider returned an invalid result.",
            ) from exc
