import asyncio
import codecs
import logging
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin

import aiohttp
from aiohttp.abc import AbstractResolver

from .config import ResearchSettings
from .models import ResearchIssue, SearchResult, SourceContent
from .urls import UnsafeURL, require_public_ip, validate_web_url

logger = logging.getLogger(__name__)
_REMOVED_TAGS = {
    "aside",
    "footer",
    "form",
    "header",
    "iframe",
    "nav",
    "noscript",
    "script",
    "style",
    "svg",
    "template",
}
_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


class PublicDNSResolver(AbstractResolver):
    """Resolve and validate addresses in the connector path to prevent DNS rebinding."""

    def __init__(
        self,
        lookup: Callable[[str, int], Any] | None = None,
    ) -> None:
        self._lookup = lookup

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        if self._lookup is None:
            entries = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
        else:
            entries = await self._lookup(host, port)

        resolved: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for entry in entries:
            address_info = entry[4]
            ip = address_info[0]
            require_public_ip(ip)
            entry_family = entry[0]
            key = (entry_family, ip)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": entry_family,
                    "proto": entry[2],
                    "flags": entry[3],
                }
            )
        if not resolved:
            raise UnsafeURL("The source hostname did not resolve to a public IP address.")
        return resolved

    async def close(self) -> None:
        return None


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._removed_depth = 0
        self._title_depth = 0
        self._date_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.published_at: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        if tag in _REMOVED_TAGS:
            self._removed_depth += 1
            return
        if self._removed_depth:
            return
        if tag == "title":
            self._title_depth += 1
        if tag == "time":
            self._date_depth += 1
            self.published_at = self.published_at or attributes.get("datetime")
        if tag == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            ).casefold()
            if key in {
                "article:published_time",
                "date",
                "datepublished",
                "pubdate",
            }:
                self.published_at = self.published_at or attributes.get("content")
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _REMOVED_TAGS:
            self._removed_depth = max(0, self._removed_depth - 1)
            return
        if self._removed_depth:
            return
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        if tag == "time":
            self._date_depth = max(0, self._date_depth - 1)
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title_parts.append(data)
        if not self._removed_depth and not self._title_depth:
            self.text_parts.append(data)
            if self._date_depth and self.published_at is None:
                self.published_at = data.strip() or None


class SourceExtractionService:
    def __init__(
        self,
        settings: ResearchSettings,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    async def extract(self, result: SearchResult) -> tuple[SourceContent | None, ResearchIssue | None]:
        original_url = str(result.url)
        try:
            validate_web_url(original_url)
            page_bytes, final_url, was_truncated, charset = await self._download(original_url)
            parser = _ReadableHTMLParser()
            parser.feed(page_bytes.decode(charset, errors="replace"))
            text = " ".join(" ".join(parser.text_parts).split())
            text = text[: self._settings.source_max_chars]
            if not text:
                return None, ResearchIssue(
                    code="empty_source",
                    message="The webpage did not contain readable text.",
                    source_url=result.url,
                )
            return (
                SourceContent(
                    url=result.url,
                    final_url=final_url,
                    title=" ".join(" ".join(parser.title_parts).split()) or result.title,
                    domain=result.domain,
                    published_at=parser.published_at or result.published_at,
                    text=text,
                    retrieved_at=datetime.now(timezone.utc),
                    truncated=was_truncated or len(" ".join(" ".join(parser.text_parts).split()))
                    > self._settings.source_max_chars,
                ),
                None,
            )
        except UnsafeURL:
            logger.info("Rejected unsafe source URL.")
            return None, ResearchIssue(
                code="unsafe_source_url",
                message="The webpage URL did not pass public-network validation.",
                source_url=result.url,
            )
        except asyncio.TimeoutError:
            return None, ResearchIssue(
                code="source_timeout",
                message="The webpage request timed out.",
                source_url=result.url,
            )
        except aiohttp.ClientError:
            return None, ResearchIssue(
                code="source_unavailable",
                message="The webpage could not be retrieved.",
                source_url=result.url,
            )
        except (UnicodeError, LookupError, ValueError):
            return None, ResearchIssue(
                code="source_unavailable",
                message="The webpage response could not be read.",
                source_url=result.url,
            )

    async def _download(self, initial_url: str) -> tuple[bytes, str, bool, str]:
        timeout = aiohttp.ClientTimeout(total=self._settings.source_timeout_seconds)
        connector = aiohttp.TCPConnector(
            resolver=PublicDNSResolver(),
            use_dns_cache=False,
            limit=1,
        )
        current_url = initial_url
        async with self._session_factory(connector=connector, timeout=timeout) as session:
            for redirect_count in range(self._settings.source_max_redirects + 1):
                validate_web_url(current_url)
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers={"User-Agent": "RMatrixResearch/1.0"},
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirect_count >= self._settings.source_max_redirects:
                            raise aiohttp.ClientResponseError(
                                response.request_info,
                                response.history,
                                status=response.status,
                                message="Redirect limit reached or location missing.",
                                headers=response.headers,
                            )
                        current_url = urljoin(current_url, location)
                        validate_web_url(current_url)
                        continue
                    if response.status != 200:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message="Webpage request failed.",
                            headers=response.headers,
                        )
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message="Webpage is not HTML.",
                            headers=response.headers,
                        )
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > self._settings.source_max_bytes:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=413,
                            message="Webpage exceeds the configured size limit.",
                            headers=response.headers,
                        )
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        remaining = self._settings.source_max_bytes - size
                        if len(chunk) > remaining:
                            chunks.append(chunk[:remaining])
                            truncated = True
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    charset = response.charset or "utf-8"
                    codecs.lookup(charset)
                    return b"".join(chunks), current_url, truncated, charset
        raise aiohttp.ClientError("Could not retrieve webpage.")
