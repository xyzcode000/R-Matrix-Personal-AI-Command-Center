import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class ResearchSettings:
    tavily_api_key: str = field(default="", repr=False)
    search_timeout_seconds: float = 10.0
    source_timeout_seconds: float = 8.0
    default_result_limit: int = 5
    max_result_limit: int = 10
    source_max_bytes: int = 1_000_000
    source_max_chars: int = 12_000
    source_max_redirects: int = 3

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "ResearchSettings":
        env = os.environ if environ is None else environ
        settings = cls(
            tavily_api_key=env.get("TAVILY_API_KEY", "").strip(),
            search_timeout_seconds=_read_float(env, "WEB_SEARCH_TIMEOUT_SECONDS", 10.0),
            source_timeout_seconds=_read_float(env, "WEB_SOURCE_TIMEOUT_SECONDS", 8.0),
            default_result_limit=_read_int(env, "WEB_SEARCH_DEFAULT_LIMIT", 5),
            max_result_limit=_read_int(env, "WEB_SEARCH_MAX_LIMIT", 10),
            source_max_bytes=_read_int(env, "WEB_SOURCE_MAX_BYTES", 1_000_000),
            source_max_chars=_read_int(env, "WEB_SOURCE_MAX_CHARS", 12_000),
            source_max_redirects=_read_int(env, "WEB_SOURCE_MAX_REDIRECTS", 3),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 0.1 <= self.search_timeout_seconds <= 60:
            raise ValueError("WEB_SEARCH_TIMEOUT_SECONDS must be between 0.1 and 60.")
        if not 0.1 <= self.source_timeout_seconds <= 60:
            raise ValueError("WEB_SOURCE_TIMEOUT_SECONDS must be between 0.1 and 60.")
        if not 1 <= self.default_result_limit <= self.max_result_limit <= 10:
            raise ValueError("Web search result limits must be between 1 and 10.")
        if not 1_024 <= self.source_max_bytes <= 5_000_000:
            raise ValueError("WEB_SOURCE_MAX_BYTES must be between 1024 and 5000000.")
        if not 100 <= self.source_max_chars <= 100_000:
            raise ValueError("WEB_SOURCE_MAX_CHARS must be between 100 and 100000.")
        if not 0 <= self.source_max_redirects <= 5:
            raise ValueError("WEB_SOURCE_MAX_REDIRECTS must be between 0 and 5.")


def _read_int(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _read_float(env: dict[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
