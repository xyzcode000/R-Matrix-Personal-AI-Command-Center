import asyncio
import json
import socket
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from app.research.config import ResearchSettings
from app.research.extractor import PublicDNSResolver, SourceExtractionService
from app.research.formatter import ResearchResponseFormatter
from app.research.models import ResearchIssue, ResearchResponse, SearchResult
from app.research.providers import (
    SearchProviderError,
    TavilySearchProvider,
)
from app.research.service import WebResearchService
from app.research.urls import UnsafeURL, validate_web_url


def make_settings(**overrides: Any) -> ResearchSettings:
    values: dict[str, Any] = {"tavily_api_key": "test-key"}
    values.update(overrides)
    return ResearchSettings(**values)


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class StaticProvider:
    def __init__(self, results: list[SearchResult] | None = None, error: Exception | None = None):
        self.results = results or []
        self.error = error

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        if self.error:
            raise self.error
        return self.results[:limit]


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.content = FakeContent(chunks or [])
        self.request_info = None
        self.history: list[Any] = []
        self.charset = "utf-8"

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self.chunks:
            yield chunk


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.urls.append(url)
        return self.responses.pop(0)


def result(url: str = "https://example.org/article") -> SearchResult:
    return SearchResult(
        title="Example Article",
        url=url,
        snippet="A short result.",
        domain="example.org",
    )


def test_tavily_search_maps_results_and_auth_header() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Current result",
                        "url": "https://example.org/news",
                        "content": "Search snippet",
                        "published_date": "2026-11-05",
                        "score": 0.9,
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = TavilySearchProvider(make_settings(), client=client)
    found = run(provider.search("current topic", 3))
    run(client.aclose())

    assert len(found) == 1
    assert found[0].title == "Current result"
    assert found[0].snippet == "Search snippet"
    assert found[0].published_at == "2026-11-05"
    assert found[0].domain == "example.org"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(requests[0].content)["max_results"] == 3


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "search_rate_limited"), (503, "search_provider_error")],
)
def test_tavily_provider_maps_http_failures(status: int, code: str) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    )
    provider = TavilySearchProvider(make_settings(), client=client)
    with pytest.raises(SearchProviderError) as error:
        run(provider.search("query", 2))
    run(client.aclose())
    assert error.value.code == code


def test_tavily_provider_maps_timeout() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private request detail")

    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    provider = TavilySearchProvider(make_settings(), client=client)
    with pytest.raises(SearchProviderError) as error:
        run(provider.search("query", 2))
    run(client.aclose())
    assert error.value.code == "search_timeout"
    assert "private request detail" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"[]", b'{"results": "bad"}', b'{"results": [null]}'],
)
def test_tavily_provider_rejects_malformed_responses(body: bytes) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body)
        )
    )
    provider = TavilySearchProvider(make_settings(), client=client)
    with pytest.raises(SearchProviderError) as error:
        run(provider.search("query", 2))
    run(client.aclose())
    assert error.value.code == "malformed_search_response"


def test_tavily_provider_requires_api_key() -> None:
    provider = TavilySearchProvider(make_settings(tavily_api_key=""))
    with pytest.raises(SearchProviderError) as error:
        run(provider.search("query", 2))
    assert error.value.code == "provider_not_configured"


def test_research_settings_read_environment_and_validate_limits() -> None:
    settings = ResearchSettings.from_env(
        {
            "TAVILY_API_KEY": "configured-key",
            "WEB_SEARCH_DEFAULT_LIMIT": "3",
            "WEB_SEARCH_MAX_LIMIT": "7",
            "WEB_SOURCE_MAX_CHARS": "500",
        }
    )
    assert settings.tavily_api_key == "configured-key"
    assert settings.default_result_limit == 3
    assert settings.max_result_limit == 7
    assert settings.source_max_chars == 500
    with pytest.raises(ValueError, match="between 1 and 10"):
        ResearchSettings.from_env(
            {
                "WEB_SEARCH_DEFAULT_LIMIT": "5",
                "WEB_SEARCH_MAX_LIMIT": "2",
            }
        )


def test_research_rejects_empty_query_and_invalid_limit() -> None:
    service = WebResearchService(make_settings(), search_provider=StaticProvider())
    with pytest.raises(ValueError, match="must not be empty"):
        run(service.research("  "))
    with pytest.raises(ValueError, match="limit"):
        run(service.research("query", limit=11))


def test_research_provider_failure_is_reported_in_response() -> None:
    service = WebResearchService(
        make_settings(),
        search_provider=StaticProvider(error=SearchProviderError("offline", "Provider offline.")),
    )
    response = run(service.research("query"))
    assert response.query == "query"
    assert response.results == []
    assert response.errors[0].code == "offline"


def test_research_enforces_requested_result_limit_for_injected_provider() -> None:
    service = WebResearchService(
        make_settings(),
        search_provider=StaticProvider([result(), result("https://example.net/"), result("https://example.com/")]),
    )
    response = run(service.research("query", limit=1, extract_sources=False))
    assert len(response.results) == 1


def test_source_extraction_removes_irrelevant_content_and_attributes_source() -> None:
    html = (
        b"<html><head><title>Article title</title>"
        b'<meta property="article:published_time" content="2026-11-04"></head>'
        b"<body><nav>Navigation</nav><main><p>Readable evidence.</p></main>"
        b"<script>secret()</script></body></html>"
    )
    fake_session = FakeSession([FakeResponse(chunks=[html])])
    extractor = SourceExtractionService(
        make_settings(),
        session_factory=lambda **kwargs: fake_session,
    )
    content, warning = run(extractor.extract(result()))
    assert warning is None
    assert content is not None
    assert content.url == result().url
    assert content.final_url == result().url
    assert content.title == "Article title"
    assert content.published_at == "2026-11-04"
    assert content.text == "Readable evidence."
    assert content.is_untrusted is True
    assert fake_session.urls == ["https://example.org/article"]


def test_source_extraction_blocks_invalid_url_before_network() -> None:
    called = False

    def factory(**kwargs: Any) -> FakeSession:
        nonlocal called
        called = True
        return FakeSession([])

    extractor = SourceExtractionService(make_settings(), session_factory=factory)
    content, warning = run(extractor.extract(result("http://127.0.0.1/admin")))
    assert content is None
    assert warning is not None
    assert warning.code == "unsafe_source_url"
    assert called is False


def test_source_extraction_blocks_redirect_to_private_url() -> None:
    fake_session = FakeSession(
        [
            FakeResponse(
                status=302,
                headers={"Location": "http://127.0.0.1/admin"},
            )
        ]
    )
    extractor = SourceExtractionService(
        make_settings(),
        session_factory=lambda **kwargs: fake_session,
    )
    content, warning = run(extractor.extract(result()))
    assert content is None
    assert warning is not None
    assert warning.code == "unsafe_source_url"
    assert fake_session.urls == ["https://example.org/article"]


def test_source_extraction_reports_unavailable_page() -> None:
    fake_session = FakeSession([FakeResponse(status=404)])
    extractor = SourceExtractionService(
        make_settings(),
        session_factory=lambda **kwargs: fake_session,
    )
    content, warning = run(extractor.extract(result()))
    assert content is None
    assert warning is not None
    assert warning.code == "source_unavailable"


def test_source_extraction_honors_content_size_limit() -> None:
    html = b"<p>" + (b"x" * 1500) + b"</p>"
    fake_session = FakeSession([FakeResponse(chunks=[html])])
    extractor = SourceExtractionService(
        make_settings(source_max_bytes=1024),
        session_factory=lambda **kwargs: fake_session,
    )
    content, warning = run(extractor.extract(result()))
    assert warning is None
    assert content is not None
    assert len(content.text) <= 1024
    assert content.truncated is True


def test_source_extraction_reports_timeout() -> None:
    class TimeoutSession(FakeSession):
        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            raise asyncio.TimeoutError

    extractor = SourceExtractionService(
        make_settings(),
        session_factory=lambda **kwargs: TimeoutSession([]),
    )
    content, warning = run(extractor.extract(result()))
    assert content is None
    assert warning is not None
    assert warning.code == "source_timeout"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://service.local/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:pass@example.org/",
        "https://example.org:8443/",
    ],
)
def test_url_validation_rejects_local_or_non_web_targets(url: str) -> None:
    with pytest.raises(UnsafeURL):
        validate_web_url(url)


def test_public_dns_resolver_rejects_private_dns_answers() -> None:
    async def lookup(host: str, port: int) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))
        ]

    with pytest.raises(UnsafeURL):
        run(PublicDNSResolver(lookup).resolve("example.org", 443))


def test_public_dns_resolver_rejects_mixed_public_and_private_answers() -> None:
    async def lookup(host: str, port: int) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.2", port)),
        ]

    with pytest.raises(UnsafeURL):
        run(PublicDNSResolver(lookup).resolve("example.org", 443))


def test_public_dns_resolver_returns_only_validated_public_addresses() -> None:
    async def lookup(host: str, port: int) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))
        ]

    resolved = run(PublicDNSResolver(lookup).resolve("example.org", 443))
    assert resolved[0]["host"] == "8.8.8.8"
    assert resolved[0]["port"] == 443


def test_research_formatter_marks_external_content_untrusted_and_keeps_citation() -> None:
    response = ResearchResponse(
        query="query",
        results=[result()],
        sources=[],
        retrieved_at=datetime.now(timezone.utc),
    )
    formatted = ResearchResponseFormatter.format_for_model(response)
    assert "untrusted external data" in formatted
    assert "https://example.org/article" in formatted
    assert '"query":"query"' in formatted
    assert '"citation":"[Web Source 1]"' in formatted


def test_research_formatter_bounds_results_and_excerpt_text() -> None:
    response = ResearchResponse(
        query="query",
        results=[
            result(),
            result("https://example.net/article"),
        ],
        sources=[],
        retrieved_at=datetime.now(timezone.utc),
    )
    formatted = ResearchResponseFormatter.format_for_model(
        response,
        max_results=1,
        excerpt_chars=5,
        max_total_chars=5,
    )
    assert formatted.count('"citation":') == 1
    assert '"excerpt":"A sho"' in formatted


def test_research_extracts_sources_and_preserves_partial_failure_warning() -> None:
    source_result = result()

    class Extractor:
        async def extract(self, search_result: SearchResult):
            return None, ResearchIssue(
                code="source_unavailable",
                message="Unavailable.",
                source_url=search_result.url,
            )

    class PartialExtractor(SourceExtractionService):
        async def extract(self, search_result: SearchResult):
            return await Extractor().extract(search_result)

    service = WebResearchService(
        make_settings(),
        search_provider=StaticProvider([source_result]),
        source_extractor=PartialExtractor(make_settings()),
    )
    response = run(service.research("query"))
    assert response.results[0].url == source_result.url
    assert response.warnings[0].source_url == source_result.url
