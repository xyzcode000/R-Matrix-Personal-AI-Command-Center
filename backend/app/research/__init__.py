"""Independent web research services for future agent integration."""

from .config import ResearchSettings
from .models import ResearchResponse, SearchResult, SourceContent
from .service import WebResearchService

__all__ = [
    "ResearchSettings",
    "ResearchResponse",
    "SearchResult",
    "SourceContent",
    "WebResearchService",
]
