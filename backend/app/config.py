import os
import re
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    host: str = os.getenv("APP_HOST", "127.0.0.1")
    port: int = int(os.getenv("APP_PORT", "8000"))
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
    model_provider: str = os.getenv("AI_PROVIDER", os.getenv("MODEL_PROVIDER", "ollama")).strip().lower()
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    smtp_host: str = os.getenv("SMTP_HOST", "").strip()
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "").strip()
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_enabled: bool = os.getenv("TELEGRAM_ENABLED", "true").strip().lower() == "true"
    telegram_poll_timeout: int = int(os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "25"))
    telegram_pairing_ttl_minutes: int = int(os.getenv("TELEGRAM_PAIRING_TTL_MINUTES", "10"))
    auth_jwt_secret: str = os.getenv("AUTH_JWT_SECRET", "").strip()
    auth_cookie_secure: bool = os.getenv("AUTH_COOKIE_SECURE", "false").strip().lower() == "true"
    auth_session_hours: int = int(os.getenv("AUTH_SESSION_HOURS", "12"))
    control_plane_only: bool = os.getenv("CONTROL_PLANE_ONLY", "false").strip().lower() == "true"
    auth_telegram_otp_enabled: bool = os.getenv("AUTH_TELEGRAM_OTP_ENABLED", "true").strip().lower() == "true"
    auth_otp_ttl_minutes: int = int(os.getenv("AUTH_OTP_TTL_MINUTES", "5"))
    auth_otp_resend_seconds: int = int(os.getenv("AUTH_OTP_RESEND_SECONDS", "30"))
    auth_otp_max_attempts: int = int(os.getenv("AUTH_OTP_MAX_ATTEMPTS", "5"))
    require_api_auth: bool = os.getenv("REQUIRE_API_AUTH", "true").strip().lower() == "true"
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    intelligence_scan_interval_minutes: int = int(os.getenv("INTELLIGENCE_SCAN_INTERVAL_MINUTES", "5"))
    intelligence_max_messages_per_scan: int = int(os.getenv("INTELLIGENCE_MAX_MESSAGES_PER_SCAN", "150"))
    document_chunk_size: int = int(os.getenv("DOCUMENT_CHUNK_SIZE", "1000"))
    document_chunk_overlap: int = int(os.getenv("DOCUMENT_CHUNK_OVERLAP", "150"))
    # Local OCR infrastructure. TESSERACT_CMD/TESSDATA_PREFIX take precedence over
    # automatic discovery; both are optional because the resolver also probes PATH
    # and the standard Windows installation folders.
    tesseract_cmd: str = os.getenv("TESSERACT_CMD", "").strip()
    tessdata_prefix: str = os.getenv("TESSDATA_PREFIX", "").strip()
    document_ocr_languages: str = os.getenv("DOCUMENT_OCR_LANGUAGES", "eng,hin").strip()
    document_ocr_max_pages: int = int(os.getenv("DOCUMENT_OCR_MAX_PAGES", "10"))
    document_ocr_dpi: int = int(os.getenv("DOCUMENT_OCR_DPI", "200"))
    document_ocr_page_timeout: int = int(os.getenv("DOCUMENT_OCR_PAGE_TIMEOUT_SECONDS", "25"))
    document_ocr_total_timeout: int = int(os.getenv("DOCUMENT_OCR_TOTAL_TIMEOUT_SECONDS", "300"))
    document_native_text_min_chars: int = int(os.getenv("DOCUMENT_NATIVE_TEXT_MIN_CHARS", "40"))
    document_max_pdf_pages: int = int(os.getenv("DOCUMENT_MAX_PDF_PAGES", "100"))
    authorized_project_dirs_raw: str = os.getenv(
        "AUTHORIZED_PROJECT_DIRS",
        str(Path(__file__).resolve().parents[2]),
    )
    data_dir: Path = Path(
        os.getenv(
            "R_MATRIX_DATA_DIR",
            str(Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "RMatrixCommandCenter"),
        )
    ).expanduser().resolve()

    def __post_init__(self) -> None:
        self.host = self.host.strip()
        if not _is_loopback_host(self.host):
            raise ValueError("APP_HOST must be localhost or a loopback address; public interfaces are disabled.")

        self.frontend_origin = self.frontend_origin.strip().rstrip("/")
        try:
            origin = urlsplit(self.frontend_origin)
            origin.port
        except ValueError as exc:
            raise ValueError("FRONTEND_ORIGIN must be a valid localhost or loopback origin.") from exc
        if (
            origin.scheme not in {"http", "https"}
            or not _is_loopback_host(origin.hostname)
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("FRONTEND_ORIGIN must be a valid localhost or loopback origin.")

        if self.model_provider == "google":
            self.model_provider = "gemini"
        if not 1 <= self.auth_session_hours <= 168:
            raise ValueError("AUTH_SESSION_HOURS must be between 1 and 168.")
        if self.auth_jwt_secret and len(self.auth_jwt_secret.encode("utf-8")) < 32:
            raise ValueError("AUTH_JWT_SECRET must contain at least 32 bytes.")
        if not 100 <= self.document_chunk_size <= 10000:
            raise ValueError("DOCUMENT_CHUNK_SIZE must be between 100 and 10000.")
        if not 0 <= self.document_chunk_overlap < self.document_chunk_size:
            raise ValueError("DOCUMENT_CHUNK_OVERLAP must be smaller than DOCUMENT_CHUNK_SIZE.")
        if not 1 <= self.document_ocr_max_pages <= 100:
            raise ValueError("DOCUMENT_OCR_MAX_PAGES must be between 1 and 100.")
        if not 1 <= self.document_max_pdf_pages <= 500:
            raise ValueError("DOCUMENT_MAX_PDF_PAGES must be between 1 and 500.")
        if not 10 <= self.document_ocr_dpi <= 400:
            raise ValueError("DOCUMENT_OCR_DPI must be between 10 and 400.")
        if not 1 <= self.document_ocr_page_timeout <= 120:
            raise ValueError("DOCUMENT_OCR_PAGE_TIMEOUT_SECONDS must be between 1 and 120.")
        if not 1 <= self.document_native_text_min_chars <= 500:
            raise ValueError("DOCUMENT_NATIVE_TEXT_MIN_CHARS must be between 1 and 500.")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535.")
        if self.intelligence_scan_interval_minutes < 0:
            raise ValueError("INTELLIGENCE_SCAN_INTERVAL_MINUTES cannot be negative.")
        if not 1 <= self.intelligence_max_messages_per_scan <= 5000:
            raise ValueError("INTELLIGENCE_MAX_MESSAGES_PER_SCAN must be between 1 and 5000.")
        if not 1 <= self.telegram_poll_timeout <= 50:
            raise ValueError("TELEGRAM_POLL_TIMEOUT_SECONDS must be between 1 and 50.")
        if not 1 <= self.auth_otp_ttl_minutes <= 15:
            raise ValueError("AUTH_OTP_TTL_MINUTES must be between 1 and 15.")
        if not 10 <= self.auth_otp_resend_seconds <= 300:
            raise ValueError("AUTH_OTP_RESEND_SECONDS must be between 10 and 300.")
        if not 3 <= self.auth_otp_max_attempts <= 10:
            raise ValueError("AUTH_OTP_MAX_ATTEMPTS must be between 3 and 10.")
        if not 1 <= self.telegram_pairing_ttl_minutes <= 60:
            raise ValueError("TELEGRAM_PAIRING_TTL_MINUTES must be between 1 and 60.")

    @property
    def smtp_configured(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_from
            and ((self.smtp_username and self.smtp_password) or (not self.smtp_username and not self.smtp_password))
        )

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def requested_ocr_languages(self) -> list[str]:
        languages = [
            item.strip().casefold()
            for item in re.split(r"[,+\s]+", self.document_ocr_languages)
            if item.strip()
        ]
        # English is the baseline language for mixed Indian documents.
        if "eng" not in languages:
            languages.insert(0, "eng")
        return list(dict.fromkeys(languages))

    @property
    def database_path(self) -> Path:
        return self.data_dir / "r_matrix.sqlite3"

    @property
    def control_database_path(self) -> Path:
        return self.data_dir / "control_plane.sqlite3"

    @property
    def authorized_project_dirs(self) -> dict[str, Path]:
        roots: dict[str, Path] = {}
        for configured in self.authorized_project_dirs_raw.split(os.pathsep):
            candidate = configured.strip()
            if not candidate:
                continue
            path = Path(candidate).expanduser().resolve()
            if path.is_dir():
                roots[path.name.casefold()] = path
        return roots

    @property
    def ollama_url_is_local(self) -> bool:
        try:
            parsed = urlsplit(self.ollama_base_url)
            return (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    @property
    def frontend_origins(self) -> list[str]:
        origin = self.frontend_origin.rstrip("/")
        aliases = {
            "http://localhost:3000": "http://127.0.0.1:3000",
            "http://127.0.0.1:3000": "http://localhost:3000",
        }
        return [origin, aliases[origin]] if origin in aliases else [origin]


settings = Settings()
