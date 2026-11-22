import asyncio
import logging
from datetime import datetime, timezone

from .config import ResearchSettings
from .extractor import SourceExtractionService
from .models import ResearchIssue, ResearchResponse, SearchResult, SourceContent
from .providers import SearchProvider, SearchProviderError, TavilySearchProvider

logger = logging.getLogger(__name__)


class WebResearchService:
    def __init__(
        self,
        settings: ResearchSettings,
        search_provider: SearchProvider | None = None,
        source_extractor: SourceExtractionService | None = None,
    ) -> None:
        settings.validate()
        self._settings = settings
        self._search_provider = search_provider or TavilySearchProvider(settings)
        self._source_extractor = source_extractor or SourceExtractionService(settings)

    async def research(
        self,
        query: str,
        *,
        limit: int | None = None,
        extract_sources: bool = True,
    ) -> ResearchResponse:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Search query must not be empty.")
        if len(normalized_query) > 1000:
            raise ValueError("Search query must not exceed 1000 characters.")
        result_limit = self._settings.default_result_limit if limit is None else limit
        if not 1 <= result_limit <= self._settings.max_result_limit:
            raise ValueError(
                f"Search result limit must be between 1 and {self._settings.max_result_limit}."
            )

        results = []
        sources = []
        errors: list[ResearchIssue] = []
        warnings: list[ResearchIssue] = []
        try:
            results = (await self._search_provider.search(normalized_query, result_limit))[
                :result_limit
            ]
        except SearchProviderError as exc:
            logger.warning(
                "Web search failed.",
                extra={"event": "web_research_search_failed", "error_code": exc.code},
            )
            errors.append(ResearchIssue(code=exc.code, message=str(exc)))

        if extract_sources and results:
            semaphore = asyncio.Semaphore(3)

            async def extract_bounded(
                result: SearchResult,
            ) -> tuple[SourceContent | None, ResearchIssue | None]:
                async with semaphore:
                    return await self._source_extractor.extract(result)

            extracted = await asyncio.gather(*(extract_bounded(result) for result in results))
            for source, issue in extracted:
                if source is not None:
                    sources.append(source)
                if issue is not None:
                    warnings.append(issue)

        return ResearchResponse(
            query=normalized_query,
            results=results,
            sources=sources,
            retrieved_at=datetime.now(timezone.utc),
            errors=errors,
            warnings=warnings,
        )
