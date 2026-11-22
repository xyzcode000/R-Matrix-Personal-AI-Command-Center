from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchResult(ResearchModel):
    title: str = Field(min_length=1, max_length=1000)
    url: HttpUrl
    snippet: str = Field(default="", max_length=10_000)
    published_at: str | None = Field(default=None, max_length=200)
    domain: str = Field(min_length=1, max_length=253)
    score: float | None = Field(default=None, ge=0, le=1)


class SourceContent(ResearchModel):
    url: HttpUrl
    final_url: HttpUrl | None = None
    title: str | None = Field(default=None, max_length=1000)
    domain: str = Field(min_length=1, max_length=253)
    published_at: str | None = Field(default=None, max_length=200)
    text: str = Field(max_length=100_000)
    retrieved_at: datetime
    truncated: bool = False
    is_untrusted: Literal[True] = True


class ResearchIssue(ResearchModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    source_url: HttpUrl | None = None


class ResearchResponse(ResearchModel):
    query: str = Field(min_length=1, max_length=1000)
    results: list[SearchResult] = Field(default_factory=list)
    sources: list[SourceContent] = Field(default_factory=list)
    retrieved_at: datetime
    errors: list[ResearchIssue] = Field(default_factory=list)
    warnings: list[ResearchIssue] = Field(default_factory=list)
