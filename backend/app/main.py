from contextlib import asynccontextmanager
import asyncio
import json
import logging
import sqlite3
import hashlib
import re
import smtplib
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from . import agents, control_plane, database, document_pipeline, documents, intelligence, log_redaction, ollama, providers, reminders, telegram
from .config import settings

log_redaction.install_log_redaction()

logger = logging.getLogger(__name__)
from .research.config import ResearchSettings
from .research.formatter import ResearchResponseFormatter
from .research.models import ResearchResponse
from .research.service import WebResearchService
from .schemas import (
    ApprovalProposalRequest,
    AgentEnabledRequest,
    ChatRequest,
    ChatResponse,
    DocumentBulkDeleteRequest,
    DocumentMetadataUpdate,
    DocumentSearchRequest,
    IntelligenceReviewRequest,
    IntelligenceScanRequest,
    MemoryClearRequest,
    MemoryProposalRequest,
    MemoryReviewRequest,
    MemoryUpdateRequest,
    OwnerProfileUpdateRequest,
    ProjectAliasRequest,
    ProjectCreateRequest,
    ProjectMilestoneRequest,
    ProjectNoteRequest,
    ProjectUpdateRequest,
    ReminderRecipientRequest,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatusRequest,
    TaskUpdateRequest,
    SENSITIVE_CREDENTIAL_PATTERN,
)
from .time_context import answer_simple_time_question, current_time


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.control_plane_only:
        with database.session() as connection:
            settings.documents_dir.mkdir(parents=True, exist_ok=True)
            recovered = documents.recover_interrupted_jobs(connection)
            if recovered:
                logger.warning("Marked %s interrupted document job(s) as recoverable.", recovered)
            documents.backfill_document_types(connection)
        # Printed (not only logged) so readiness is visible in the uvicorn console
        # even when application INFO logging is filtered.
        print(document_pipeline.startup_summary(), flush=True)
    control_plane.initialize()
    reminder_stop = asyncio.Event()
    reminder_task = asyncio.create_task(reminders.reminder_worker(reminder_stop))
    intelligence_stop = asyncio.Event()
    intelligence_task = asyncio.create_task(intelligence.intelligence_worker(intelligence_stop))
    telegram_stop = asyncio.Event()
    telegram_task = asyncio.create_task(telegram.polling_worker(telegram_stop))
    try:
        yield
    finally:
        reminder_stop.set()
        intelligence_stop.set()
        telegram_stop.set()
        await reminder_task
        await intelligence_task
        await telegram_task


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class OtpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=8, max_length=64)
    code: str = Field(min_length=4, max_length=12)


class OtpChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=8, max_length=64)


class EnrollmentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    code: str = Field(min_length=20, max_length=100)
    name: str = Field(min_length=1, max_length=80)


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=64)
    operation: str = Field(pattern=r"^(health\.check|agent\.status)$")


class JobResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(succeeded|failed)$")
    result: dict = Field(default_factory=dict)


class ProjectSearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(min_length=1, max_length=500)


class ResearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)
    extract_sources: bool = True


_WEB_SEARCH_EXPLICIT = re.compile(
    r"\b(?:search (?:the )?web|search online|web search|browse (?:the )?web|browse online|"
    r"look up(?: online)?|find online|google)\b",
    re.IGNORECASE,
)
_WEB_SEARCH_OPT_OUT = re.compile(
    r"\b(?:don't|do not|never|avoid|without)\b.{0,30}\b(?:web|internet|online|browse|search)\b"
    r"|\bno (?:web|internet|online)(?:\s+(?:search|access|browsing))?\b|\boffline\b",
    re.IGNORECASE,
)
_WEB_TIME_SENSITIVE = re.compile(
    r"\b(?:latest|newest|recent(?:ly)?|current(?:ly)?|today|right now|as of|this week|this month|"
    r"this year|202[4-9])\b",
    re.IGNORECASE,
)
_WEB_LIVE_TOPICS = re.compile(
    r"\b(?:news|headlines|current events|weather|stock(?:s)?|market|election|scores?|"
    r"pricing|price|cost|availability|available|president|prime minister|ceo|leader)\b",
    re.IGNORECASE,
)
_WEB_SOFTWARE_CURRENT = re.compile(
    r"\b(?:software|release|released|version|changelog|documentation|docs|api|sdk|"
    r"benchmark|deprecation|security advisory)\b",
    re.IGNORECASE,
)
_WEB_COMPARISON = re.compile(
    r"\bmodel comparisons?\b|\b(?:compare|comparison|versus|vs\.?)\b.{0,100}"
    r"\b(?:model|llm|chatgpt|gemini|claude|gpt[- ]?\d|llama|deepseek|mistral)\b",
    re.IGNORECASE,
)
_WEB_PRIVATE_CONTEXT = re.compile(
    r"\b(?:my|mine|our|owner|profile|memories?|indexed|documents?|private|personal)\b",
    re.IGNORECASE,
)
_WEB_DATE_ONLY = re.compile(
    r"^(?:what(?:'s| is)\s+(?:the\s+)?(?:(?:current\s+)?(?:today'?s|server)?\s*)?"
    r"(?:date|day|time)(?:\s+(?:and\s+)?(?:date|day|time|today))?|"
    r"what day is (?:it|today)|today'?s date)(?:\s+in .+)?[?.!]*$",
    re.IGNORECASE,
)


def _web_research_query(message: str, mode: str, intent: str) -> str | None:
    if mode == "never" or _WEB_SEARCH_OPT_OUT.search(message):
        return None
    if intent in {"documents", "memory", "tasks", "approval"}:
        return None
    explicit = mode == "always" or bool(_WEB_SEARCH_EXPLICIT.search(message))
    if mode == "auto" and not explicit and _WEB_DATE_ONLY.fullmatch(message.strip()):
        return None
    changing_software_info = bool(
        _WEB_TIME_SENSITIVE.search(message) and _WEB_SOFTWARE_CURRENT.search(message)
    )
    live_topic = bool(_WEB_LIVE_TOPICS.search(message))
    model_comparison = bool(_WEB_COMPARISON.search(message))
    if intent == "project" and mode == "auto" and not (
        explicit or changing_software_info or live_topic or model_comparison
    ):
        return None
    if not explicit and not (
        changing_software_info
        or live_topic
        or model_comparison
    ):
        return None
    query = re.sub(r"```[\s\S]*?```|`[^`]*`", " ", message)
    if intent == "project":
        query = re.sub(
            r"\b(?:and\s+)?(?:how|what|where|why)\s+(?:(?:should|can|could|would|do)\s+)?"
            r"(?:i|we|you)\b.*$",
            " ",
            query,
            flags=re.IGNORECASE,
        )
    query = _WEB_PRIVATE_CONTEXT.sub(" ", query)
    query = re.sub(r"https?://\S+|\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b", " ", query)
    query = re.sub(
        r"\b(?:api[_-]?key|access[_-]?token|password|secret|credential)\s*[:=]\s*\S+",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b", " ", query)
    query = _WEB_SEARCH_EXPLICIT.sub(" ", query)
    query = re.sub(r"\s+", " ", query).strip(" \t\r\n-:,.")
    return query[:400].strip() or None


def _web_research_evidence(response: ResearchResponse) -> tuple[str, list[dict]]:
    retrieved_at = response.retrieved_at.isoformat()
    extracted_by_url = {str(source.url): source for source in response.sources}
    web_sources: list[dict] = []
    for index, result in enumerate(response.results[:5], start=1):
        extracted = extracted_by_url.get(str(result.url))
        excerpt = (extracted.text if extracted and extracted.text else result.snippet)[:1_200]
        source_title = result.title
        url = str(result.url)
        published_at = result.published_at or (extracted.published_at if extracted else None)
        web_sources.append(
            {
                "kind": "web",
                "title": source_title,
                "url": url,
                "domain": result.domain,
                "published_at": published_at,
                "retrieved_at": retrieved_at,
                "snippet": excerpt,
                "is_untrusted": True,
            }
        )

    status = "failed" if response.errors else "retrieved" if response.results else "no_results"
    status_source = {
        "kind": "web_research_status",
        "status": status,
        "query": response.query,
        "retrieved_at": retrieved_at,
        "errors": [issue.model_dump(mode="json") for issue in response.errors],
        "warnings": [issue.model_dump(mode="json") for issue in response.warnings],
    }
    context = ResearchResponseFormatter.format_for_model(
        response,
        max_results=5,
        excerpt_chars=1_200,
        max_total_chars=6_000,
    )
    if not response.results and response.errors:
        context = f"{context}\nCurrent information could not be verified because the research provider failed."
    elif not response.results:
        context = f"{context}\nThe web search returned no sources; current information could not be verified."
    return context, [status_source, *web_sources]


def _sanitize_web_citations(content: str, sources: list[dict]) -> str:
    web_sources = [source for source in sources if source.get("kind") == "web"]
    valid_urls = {source["url"] for source in web_sources}
    source_count = len(web_sources)

    def retain_only_known_citations(match: re.Match[str]) -> str:
        if 1 <= int(match.group(1)) <= source_count:
            return match.group(0)
        return "[unverified web citation omitted]"

    def retain_only_known_urls(match: re.Match[str]) -> str:
        value = match.group(0)
        url = value.rstrip(".,;:!?)]}")
        punctuation = value[len(url):]
        if url in valid_urls:
            return value
        return f"[link omitted: no matching retrieved source]{punctuation}"

    content = re.sub(
        r"\[Web Source\s+(\d+)\]",
        retain_only_known_citations,
        content,
        flags=re.IGNORECASE,
    )
    return re.sub(r"https?://[^\s<>\"']+", retain_only_known_urls, content, flags=re.IGNORECASE)


def _session_user(request: Request) -> dict:
    token = request.cookies.get(control_plane.SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Sign in to use remote access.")
    user = control_plane.get_session_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Your session expired. Sign in again.")
    return user


def _csrf_user(request: Request) -> dict:
    user = _session_user(request)
    cookie_token = request.cookies.get(control_plane.CSRF_COOKIE, "")
    header_token = request.headers.get("X-CSRF-Token", "")
    if not control_plane.validate_csrf(user["session_token"], cookie_token, header_token):
        raise HTTPException(status_code=403, detail="The request could not be verified. Refresh and retry.")
    return user


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/csrf",
    "/api/auth/login",
    "/api/auth/me",
    "/api/auth/2fa/status",
    "/api/auth/verify-otp",
    "/api/auth/resend-otp",
    "/api/auth/cancel-otp",
    "/api/control/enroll",
}


def _protect_api(request: Request) -> None:
    """Requires the owner session for every private API path.

    Only the health probe, the authentication endpoints and the enrolled-agent
    routes (which authenticate with their own device credential) stay public.
    """
    path = request.url.path
    if not path.startswith("/api/"):
        return
    if path.startswith("/api/control/agent/"):
        return
    if path in _PUBLIC_API_PATHS:
        return
    if not settings.require_api_auth and not path.startswith(("/api/control/", "/api/auth/")):
        return
    user = _session_user(request)
    if request.method not in _SAFE_METHODS and not control_plane.validate_csrf(
        user["session_token"],
        request.cookies.get(control_plane.CSRF_COOKIE, ""),
        request.headers.get("X-CSRF-Token", ""),
    ):
        raise HTTPException(status_code=403, detail="The request could not be verified. Refresh and retry.")


app = FastAPI(
    title="R Matrix Personal AI Command Center",
    version="0.1.0",
    description="Local API for private documents and selectable AI model providers.",
    lifespan=lifespan,
    dependencies=[Depends(_protect_api)],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
)


@app.middleware("http")
async def restrict_control_plane_deployment(request: Request, call_next):
    if settings.control_plane_only and request.url.path.startswith("/api/"):
        allowed = request.url.path.startswith(("/api/auth/", "/api/control/")) or request.url.path == "/api/health"
        if not allowed:
            return Response(status_code=404)
    return await call_next(request)


@app.get("/api/auth/csrf")
def issue_csrf_token(response: Response) -> dict[str, str]:
    token = control_plane.new_csrf_token()
    response.set_cookie(
        control_plane.CSRF_COOKIE,
        token,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        # The frontend reads this token with document.cookie, which only exposes
        # cookies whose path matches the current page; scope it to the whole origin.
        path="/",
    )
    return {"csrf_token": token}


def _require_request_csrf(request: Request) -> None:
    if not control_plane.compare_csrf(
        request.cookies.get(control_plane.CSRF_COOKIE, ""),
        request.headers.get("X-CSRF-Token", ""),
    ):
        raise HTTPException(status_code=403, detail="The request could not be verified. Refresh and retry.")


def _require_auth_configured() -> None:
    if len(settings.auth_jwt_secret.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured. Set AUTH_JWT_SECRET in backend/.env.",
        )


def _set_session_cookies(response: Response, session: dict[str, str]) -> None:
    response.set_cookie(
        control_plane.SESSION_COOKIE,
        session["token"],
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        max_age=settings.auth_session_hours * 3600,
        path="/api",
    )
    response.set_cookie(
        control_plane.CSRF_COOKIE,
        session["csrf_token"],
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        max_age=settings.auth_session_hours * 3600,
        path="/",
    )


@app.post("/api/auth/login")
async def login(request: Request, response: Response, credentials: LoginRequest) -> dict:
    """Verifies the owner password, then requires a Telegram code when one can be delivered.

    No session cookie is issued until every configured factor has passed.
    """
    _require_request_csrf(request)
    _require_auth_configured()
    account = control_plane.verify_credentials(credentials.username, credentials.password)
    if account is None:
        raise HTTPException(status_code=401, detail="Incorrect username or password, or sign-in is temporarily unavailable.")

    if not settings.auth_telegram_otp_enabled:
        # Explicit local opt-out only; never a silent fallback.
        session = control_plane.start_session(account["id"])
        if session is None:
            raise HTTPException(status_code=401, detail="Incorrect username or password, or sign-in is temporarily unavailable.")
        _set_session_cookies(response, session)
        return {"username": session["username"], "two_factor": False}

    # Mandatory two-step verification: fail closed at every incomplete step.
    if not settings.telegram_enabled or not settings.telegram_bot_token:
        raise HTTPException(
            status_code=503,
            detail=(
                "Two-step verification is enabled, but the Telegram bot is not configured. "
                "Configure TELEGRAM_BOT_TOKEN or follow the local recovery procedure."
            ),
        )
    with database.session() as connection:
        chat_id = telegram.paired_chat_id(connection)
    if chat_id is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Two-step verification requires a paired Telegram chat. Pair the bot in "
                "Settings → Telegram, or follow the local recovery procedure."
            ),
        )

    challenge = control_plane.create_otp_challenge(account["id"], account["username"])
    if challenge is None:
        raise HTTPException(
            status_code=429,
            detail="Too many verification codes were requested. Wait a few minutes and try again.",
        )
    with database.session() as connection:
        delivered = await telegram.send_login_code(
            connection, chat_id, challenge["code"], settings.auth_otp_ttl_minutes
        )
    if not delivered:
        control_plane.cancel_otp_challenge(challenge["id"])
        raise HTTPException(
            status_code=502,
            detail="The verification code could not be sent to Telegram. Try again.",
        )
    return {
        "two_factor": True,
        "challenge_id": challenge["id"],
        "expires_at": challenge["expires_at"],
        "resend_at": challenge["resend_at"],
        "delivery": "telegram",
    }


@app.get("/api/auth/me")
def current_user(request: Request) -> dict[str, str]:
    return {"username": _session_user(request)["username"]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user: dict = Depends(_csrf_user)) -> dict[str, str]:
    control_plane.revoke_session(user["session_token"])
    response.delete_cookie(control_plane.SESSION_COOKIE, path="/api", secure=settings.auth_cookie_secure, httponly=True, samesite="strict")
    response.delete_cookie(control_plane.CSRF_COOKIE, path="/", secure=settings.auth_cookie_secure, samesite="strict")
    return {"status": "signed_out"}


@app.get("/api/auth/2fa/status")
def two_factor_status() -> dict:
    """Reports whether a code can be delivered. Never exposes the chat or the token."""
    with database.session() as connection:
        available = telegram.otp_delivery_available(connection)
    return {"required": available, "channel": "telegram" if available else None}


@app.post("/api/auth/verify-otp")
def verify_otp(request: Request, response: Response, payload: OtpVerifyRequest) -> dict:
    _require_request_csrf(request)
    _require_auth_configured()
    result = control_plane.verify_otp_challenge(payload.challenge_id, payload.code)
    status_text = result["status"]
    if status_text == "verified":
        session = result["session"]
        _set_session_cookies(response, session)
        return {"username": session["username"], "two_factor": True}
    if status_text == "invalid":
        remaining = int(result.get("attempts_left", 0))
        raise HTTPException(
            status_code=401,
            detail=(
                "That verification code is incorrect."
                if remaining
                else "Too many incorrect codes. Start sign-in again."
            ),
        )
    if status_text in {"expired", "locked", "used"}:
        raise HTTPException(
            status_code=401,
            detail="That verification code is no longer valid. Start sign-in again.",
        )
    raise HTTPException(
        status_code=400,
        detail="That verification request is no longer valid. Start sign-in again.",
    )


@app.post("/api/auth/resend-otp")
async def resend_otp(request: Request, payload: OtpChallengeRequest) -> dict:
    _require_request_csrf(request)
    _require_auth_configured()
    result = control_plane.resend_otp_challenge(payload.challenge_id)
    if result["status"] == "cooldown":
        raise HTTPException(
            status_code=429,
            detail=f"Wait {result['retry_after']} seconds before requesting another code.",
        )
    if result["status"] == "limited":
        raise HTTPException(
            status_code=429,
            detail="Too many codes were sent for this sign-in. Start sign-in again.",
        )
    if result["status"] != "sent":
        raise HTTPException(
            status_code=401,
            detail="That verification request is no longer valid. Start sign-in again.",
        )
    with database.session() as connection:
        chat_id = telegram.paired_chat_id(connection)
    if chat_id is None:
        control_plane.cancel_otp_challenge(payload.challenge_id)
        raise HTTPException(
            status_code=409,
            detail="Telegram is no longer paired. Sign in again or pair the bot in Settings.",
        )
    with database.session() as connection:
        delivered = await telegram.send_login_code(
            connection, chat_id, result["code"], settings.auth_otp_ttl_minutes
        )
    if not delivered:
        control_plane.cancel_otp_challenge(payload.challenge_id)
        raise HTTPException(
            status_code=502,
            detail="The verification code could not be sent to Telegram. Try again.",
        )
    return {"expires_at": result["expires_at"], "resend_at": result["resend_at"]}


@app.post("/api/auth/cancel-otp")
def cancel_otp(request: Request, payload: OtpChallengeRequest) -> dict:
    _require_request_csrf(request)
    control_plane.cancel_otp_challenge(payload.challenge_id)
    return {"status": "cancelled"}


@app.get("/api/control/devices")
def list_devices(user: dict = Depends(_session_user)) -> list[dict]:
    return control_plane.list_devices(user["id"])


@app.post("/api/control/enrollment-codes", status_code=status.HTTP_201_CREATED)
def create_enrollment_code(user: dict = Depends(_csrf_user)) -> dict[str, str]:
    return control_plane.create_enrollment_code(user["id"])


@app.post("/api/control/enroll", status_code=status.HTTP_201_CREATED)
def enroll_device(request: EnrollmentRequest) -> dict[str, str]:
    result = control_plane.enroll_device(request.code, request.name)
    if result is None:
        raise HTTPException(status_code=400, detail="Enrollment code is invalid, expired, or already used.")
    return result


@app.post("/api/control/devices/{device_id}/revoke")
def revoke_device(device_id: str, user: dict = Depends(_csrf_user)) -> dict[str, str]:
    if not control_plane.revoke_device(user["id"], device_id):
        raise HTTPException(status_code=404, detail="Device not found.")
    return {"status": "revoked"}


def _agent_device(request: Request) -> dict:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="A device credential is required.")
    device = control_plane.get_agent_device(authorization[7:])
    if device is None:
        raise HTTPException(status_code=401, detail="The device credential is invalid or revoked.")
    return device


@app.get("/api/control/agent/jobs/next")
def next_agent_job(device: dict = Depends(_agent_device)) -> dict:
    return {"job": control_plane.claim_job(device["id"])}


@app.post("/api/control/agent/jobs/{job_id}/result")
def submit_agent_result(
    job_id: str,
    request: JobResultRequest,
    device: dict = Depends(_agent_device),
) -> dict[str, str]:
    try:
        completed = control_plane.complete_job(device["id"], job_id, request.status, request.result)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not completed:
        raise HTTPException(status_code=404, detail="Job is not active for this device.")
    return {"status": "accepted"}


@app.post("/api/control/jobs", status_code=status.HTTP_201_CREATED)
def create_job(request: JobRequest, user: dict = Depends(_csrf_user)) -> dict:
    job = control_plane.create_job(user["id"], request.device_id, request.operation)
    if job is None:
        raise HTTPException(status_code=404, detail="Device not found or revoked.")
    return job


@app.get("/api/control/jobs")
def list_jobs(user: dict = Depends(_session_user)) -> list[dict]:
    return control_plane.list_jobs(user["id"])


@app.get("/api/health")
def health() -> dict[str, object]:
    try:
        if settings.control_plane_only:
            control_plane.health_check()
        else:
            with database.session() as connection:
                connection.execute("SELECT 1")
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="Local database is unavailable.") from exc
    return {
        "status": "ok",
        "service": "R Matrix Personal AI Command Center",
        "auth_required": settings.require_api_auth,
    }


@app.get("/api/health/ollama")
async def ollama_health() -> dict:
    return await ollama.get_status()


@app.get("/api/health/model")
async def model_health() -> dict:
    provider = providers.provider_registry().get(settings.model_provider)
    if provider is None:
        return {
            "provider": settings.model_provider,
            "model": None,
            "configured": False,
            "connection_verified": False,
            "error": f"Unsupported provider '{settings.model_provider}'.",
        }
    configured, error = provider.configured()
    return {
        "provider": provider.provider_id,
        "model": provider.configured_model() if configured else None,
        "configured": configured,
        "connection_verified": False,
        "error": error,
    }


@app.get("/api/providers")
def get_providers() -> dict:
    return providers.list_provider_status()


@app.get("/api/models")
async def models(provider: str | None = Query(default=None, max_length=32)) -> dict:
    try:
        return await providers.list_models(provider)
    except providers.ProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/api/conversations")
def conversations() -> list[dict]:
    with database.session() as connection:
        return database.list_conversations(connection)


@app.get("/api/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: str) -> list[dict]:
    with database.session() as connection:
        exists = connection.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return database.get_messages(connection, conversation_id)


@app.delete("/api/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: str) -> Response:
    with database.session() as connection:
        if not database.delete_conversation(connection, conversation_id):
            raise HTTPException(status_code=404, detail="Conversation not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> dict:
    if SENSITIVE_CREDENTIAL_PATTERN.search(request.message):
        raise HTTPException(
            status_code=422,
            detail="Remove credential-like data before sending a chat message.",
        )
    intent, _ = agents.route_intent(request.message)
    if request.project_id:
        intent = "project"
    request_now = current_time()
    current_context = agents.current_server_context(now=request_now, text=request.message)
    deterministic_time_answer = (
        answer_simple_time_question(request.message, now=request_now)
        if request.web_search != "always"
        else None
    )
    web_research_context = None
    web_sources: list[dict] = []
    web_query = _web_research_query(request.message, request.web_search, intent)
    requested_provider_id = (request.provider or settings.model_provider).strip().lower()
    if requested_provider_id == "google":
        requested_provider_id = "gemini"
    provider = None
    if deterministic_time_answer is not None:
        provider = providers.provider_registry().get(requested_provider_id)
        if request.provider and provider is None:
            try:
                providers.select_provider(request.provider)
            except providers.ProviderError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    elif intent == "direct" or (
        intent == "documents"
        and (requested_provider_id == "ollama" or request.allow_cloud_document_context)
    ):
        try:
            provider = providers.select_provider(request.provider)
        except providers.ProviderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    else:
        provider = providers.provider_registry().get(requested_provider_id)
        if request.provider and provider is None:
            try:
                providers.select_provider(request.provider)
            except providers.ProviderError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    provider_id = provider.provider_id if provider else "none"
    selected_model = (
        (request.model or provider.configured_model()).strip()
        if provider
        else ""
    )
    needs_model = deterministic_time_answer is None and (
        intent == "direct"
        or request.project_id is not None
        or (
            intent == "documents"
            and (provider_id == "ollama" or request.allow_cloud_document_context)
        )
    )
    if needs_model and not selected_model:
        raise HTTPException(
            status_code=503,
            detail=f"Configure a model ID for {provider.display_name if provider else 'the selected provider'} in backend/.env.",
        )
    with database.session() as connection:
        if request.project_id:
            project = _require_project(connection, request.project_id)
            if project["workspace_id"] not in settings.authorized_project_dirs:
                raise HTTPException(status_code=409, detail="This project workspace is no longer authorized.")
        if request.allow_cloud_document_context and provider_id in {"openai", "anthropic", "gemini"}:
            if request.document_context_preview is None:
                raise HTTPException(
                    status_code=422,
                    detail="Preview and confirm the exact document excerpts before sending cloud context.",
                )
            preview = [item.model_dump() for item in request.document_context_preview]
            if preview != agents.preview_document_context(connection, request.message):
                raise HTTPException(
                    status_code=409,
                    detail="The document excerpts changed after preview. Review the updated preview and retry.",
                )
        conversation_id = request.conversation_id
        if conversation_id:
            exists = connection.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="Conversation not found.")
        else:
            conversation_id = database.create_conversation(connection, request.message)
        if web_query and database.agent_is_enabled(connection, "manager"):
            try:
                research_settings = ResearchSettings.from_env()
                research_service = WebResearchService(research_settings)
                research_response = await research_service.research(
                    web_query,
                    limit=min(5, research_settings.max_result_limit),
                    extract_sources=True,
                )
                web_research_context, web_sources = _web_research_evidence(research_response)
            except ValueError as exc:
                retrieved_at = datetime.now(timezone.utc).isoformat()
                web_research_context = (
                    "Web research could not be configured; current information could not be verified."
                )
                web_sources = [
                    {
                        "kind": "web_research_status",
                        "status": "failed",
                        "query": web_query,
                        "retrieved_at": retrieved_at,
                        "errors": [{"code": "research_configuration_error", "message": str(exc)}],
                        "warnings": [],
                    }
                ]
        # Six full turns (twelve messages) keep follow-up subjects resolvable.
        history = database.get_messages(connection, conversation_id)[-12:]
        has_pending_user = bool(history and history[-1]["role"] == "user")
        existing_pending = bool(
            has_pending_user and history[-1]["content"] == request.message
        )
        user_message = history[-1] if existing_pending else database.add_message(
            connection,
            conversation_id,
            "user",
            request.message,
            provider=provider_id if selected_model else None,
            model=selected_model or None,
        )
        try:
            result = await agents.run_manager(
                message=request.message,
                history=history,
                provider_id=provider_id,
                model=selected_model,
                allow_cloud_context=request.allow_cloud_document_context,
                document_context_preview=(
                    [item.model_dump() for item in request.document_context_preview]
                    if request.allow_cloud_document_context
                    and provider_id in {"openai", "anthropic", "gemini"}
                    and request.document_context_preview is not None
                    else None
                ),
                connection=connection,
                conversation_id=conversation_id,
                project_roots=_authorized_project_roots() if request.project_id else settings.authorized_project_dirs,
                selected_project_id=request.project_id,
                allow_cloud_project_context=request.allow_cloud_project_context,
                current_context=current_context,
                web_research_context=web_research_context,
                deterministic_time_answer=deterministic_time_answer,
            )
        except agents.ToolRegistryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except providers.ProviderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="The selected model exceeded the Manager time limit.") from exc
        assistant_content = (
            _sanitize_web_citations(result.content, web_sources)
            if web_sources
            else result.content
        )
        assistant_message = database.add_message(
            connection,
            conversation_id,
            "assistant",
            assistant_content,
            [*result.sources, *web_sources],
            provider=result.provider if result.execution["model_used"] else None,
            model=result.model if result.execution["model_used"] else None,
            execution=result.execution,
        )
    return {
        "conversation_id": conversation_id,
        "user_message": user_message,
        "message": assistant_message,
        "provider": provider.provider_id if provider and result.execution["model_used"] else None,
        "model": selected_model if result.execution["model_used"] else None,
        "document_context_included": result.document_context_included,
        "execution": result.execution,
    }


@app.get("/api/documents")
def get_documents() -> list[dict]:
    with database.session() as connection:
        return documents.list_documents(connection)


@app.get("/api/documents/health")
def document_health(force: bool = Query(default=False)) -> dict:
    """Authenticated document-processing readiness. Contains no document text."""
    with database.session() as connection:
        return documents.document_processing_health(connection, force=force)


@app.get("/api/documents/diagnostics")
def document_diagnostics() -> dict:
    """Read-only consistency report; never repairs or deletes data."""
    with database.session() as connection:
        report = documents.document_consistency(connection)
        report["health"] = documents.document_processing_health(connection)
        return report


@app.post("/api/documents/{document_id}/retry")
def retry_document(document_id: str) -> dict:
    """Re-runs extraction and OCR from the stored original file, then reindexes."""
    with database.session() as connection:
        try:
            return documents.process_document(connection, document_id)
        except HTTPException as exc:
            # The failure is already recorded on the document; return it so the
            # owner sees the real status and actionable reason.
            if exc.status_code in {400, 413, 422, 500, 504}:
                return documents.get_document(connection, document_id)
            raise


@app.post("/api/documents/{document_id}/rebuild-index")
def rebuild_document_index(document_id: str) -> dict:
    """Rebuilds chunks from already-verified extracted pages only."""
    with database.session() as connection:
        try:
            return documents.rebuild_document_index(connection, document_id)
        except HTTPException as exc:
            if exc.status_code in {400, 413, 422, 500, 504}:
                return documents.get_document(connection, document_id)
            raise


@app.post("/api/documents/upload", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    display_name: str | None = Form(default=None),
    description: str = Form(default=""),
    category: str = Form(default="Other"),
) -> dict:
    with database.session() as connection:
        return await documents.ingest_upload(
            file,
            connection,
            display_name=display_name,
            description=description,
            category=category,
        )


@app.get("/api/documents/library")
def document_library(
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=32),
    extension: str | None = Query(default=None, max_length=16),
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    sort: str = Query(default="newest", max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with database.session() as connection:
        return documents.library_documents(
            connection,
            query=q,
            category=category,
            extension=extension,
            status_filter=status_filter,
            sort=sort,
            limit=limit,
            offset=offset,
        )


@app.post("/api/documents/bulk-delete")
def bulk_delete_documents(request: DocumentBulkDeleteRequest) -> dict:
    with database.session() as connection:
        return documents.bulk_delete_documents(connection, request.document_ids)


@app.get("/api/documents/{document_id}")
def document_details(document_id: str) -> dict:
    with database.session() as connection:
        return documents.get_document(connection, document_id)


@app.patch("/api/documents/{document_id}")
def update_document(document_id: str, request: DocumentMetadataUpdate) -> dict:
    with database.session() as connection:
        return documents.update_document_metadata(
            connection,
            document_id,
            display_name=request.display_name,
            description=request.description,
            category=request.category,
        )


@app.get("/api/documents/{document_id}/content")
def document_content(document_id: str) -> dict:
    with database.session() as connection:
        return documents.get_document_text(connection, document_id)


@app.get("/api/documents/{document_id}/pages")
def document_pages(
    document_id: str,
    page: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    with database.session() as connection:
        return documents.get_document_pages(connection, document_id, page=page, limit=limit)


def _attachment_disposition(name: str) -> str:
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or "document"
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


@app.get("/api/documents/{document_id}/file")
def document_file(document_id: str, request: Request) -> Response:
    """Serves the original upload through the owner session only."""
    with database.session() as connection:
        content, media_type, name = documents.read_document_file(connection, document_id)
    headers = {
        "Content-Disposition": _attachment_disposition(name),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }
    range_header = (request.headers.get("range") or "").strip()
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
    if match and (match.group(1) or match.group(2)):
        total = len(content)
        if match.group(1):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else total - 1
        else:
            length = int(match.group(2))
            start = max(total - length, 0)
            end = total - 1
        if start >= total or start > end:
            raise HTTPException(status_code=416, detail="The requested range is not available.")
        end = min(end, total - 1)
        partial = content[start : end + 1]
        return Response(
            content=partial,
            status_code=206,
            media_type=media_type,
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Length": str(len(partial)),
            },
        )
    return Response(content=content, media_type=media_type, headers=headers)


@app.post("/api/documents/search")
def search_documents(request: DocumentSearchRequest) -> list[dict]:
    with database.session() as connection:
        return documents.search_documents(connection, request)


@app.post("/api/documents/preview")
def preview_document_context(request: ChatRequest) -> list[dict]:
    with database.session() as connection:
        return agents.preview_document_context(connection, request.message)


@app.post("/api/documents/{document_id}/reindex")
def reindex_document(document_id: str) -> dict:
    with database.session() as connection:
        return documents.reindex_document(connection, document_id)


@app.delete("/api/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_document(document_id: str) -> Response:
    with database.session() as connection:
        documents.delete_document(connection, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/agents")
def agent_catalog() -> dict:
    with database.session() as connection:
        registry = agents.build_tool_registry(
            connection,
            "",
            settings.authorized_project_dirs,
        )
        enabled = database.get_agent_settings(
            connection,
            [agent["id"] for agent in agents.AGENT_CATALOG],
        )
        stats = {
            row["agent_id"]: dict(row)
            for row in connection.execute(
                """SELECT agent_id, COUNT(*) AS execution_count,
                          SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS successful_count,
                          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                          MAX(created_at) AS last_execution_at
                   FROM agent_executions GROUP BY agent_id"""
            ).fetchall()
        }
        last_status = {
            row["agent_id"]: row["status"]
            for row in connection.execute(
                """SELECT e.agent_id, e.status FROM agent_executions e
                   JOIN (SELECT agent_id, MAX(created_at) AS latest FROM agent_executions GROUP BY agent_id) last
                   ON e.agent_id = last.agent_id AND e.created_at = last.latest"""
            ).fetchall()
        }
        return {
            "agents": [
                {
                    **agent,
                    "enabled": enabled[agent["id"]],
                    "execution_count": stats.get(agent["id"], {}).get("execution_count", 0),
                    "successful_execution_count": stats.get(agent["id"], {}).get("successful_count", 0),
                    "failed_execution_count": stats.get(agent["id"], {}).get("failed_count", 0),
                    "last_execution_at": stats.get(agent["id"], {}).get("last_execution_at"),
                    "last_execution_status": last_status.get(agent["id"]),
                }
                for agent in agents.AGENT_CATALOG
            ],
            "tools": registry.describe(),
            "authorized_project_ids": sorted(settings.authorized_project_dirs),
        }


@app.get("/api/agents/{agent_id}")
def agent_profile(agent_id: str) -> dict:
    agent = next((item for item in agents.AGENT_CATALOG if item["id"] == agent_id), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    with database.session() as connection:
        enabled = database.get_agent_settings(connection, [agent_id])[agent_id]
        registry = agents.build_tool_registry(connection, "", settings.authorized_project_dirs)
        history = [
            item for item in database.list_agent_executions(connection, 100)
            if item["agent_id"] == agent_id
        ][:20]
    tools = [
        tool for tool in registry.describe()
        if tool["required_permission"] in set(agent["permissions"])
    ]
    provider_status = providers.list_provider_status()
    selected_provider = provider_status["selected_provider"]
    return {
        "agent": {**agent, "enabled": enabled},
        "tools": tools,
        "recent_executions": history,
        "configuration": {
            "managed_by": "code-defined registry",
            "runtime_prompt_editing": False,
            "provider": selected_provider,
            "model": next(
                (
                    item["model"] for item in provider_status["providers"]
                    if item["id"] == selected_provider
                ),
                None,
            ),
        },
    }


@app.put("/api/agents/{agent_id}/enabled")
def set_agent_enabled(agent_id: str, request: AgentEnabledRequest) -> dict[str, str | bool]:
    known_agents = {agent["id"] for agent in agents.AGENT_CATALOG}
    if agent_id not in known_agents:
        raise HTTPException(status_code=404, detail="Agent not found.")
    with database.session() as connection:
        database.set_agent_enabled(connection, agent_id, request.enabled)
    return {"id": agent_id, "enabled": request.enabled}


def _authorized_project_roots() -> dict[str, Path]:
    workspaces = settings.authorized_project_dirs
    with database.session() as connection:
        database.ensure_workspace_projects(connection, workspaces)
        project_records = database.list_projects(connection)
    roots = dict(workspaces)
    for project in project_records:
        workspace = workspaces.get(project["workspace_id"])
        if workspace is not None:
            roots[project["id"].casefold()] = workspace
    return roots


@app.get("/api/projects")
def projects() -> dict:
    workspaces = settings.authorized_project_dirs
    with database.session() as connection:
        database.ensure_workspace_projects(connection, workspaces)
        records = database.list_projects(connection)
    return {
        "projects": [
            {
                **project,
                "path": str(workspaces[project["workspace_id"]]),
                "authorization_status": "authorized",
                "search_available": True,
            }
            for project in records
            if project["workspace_id"] in workspaces
        ],
        "workspaces": [
            {
                "id": workspace_id,
                "name": path.name,
                "path": str(path),
                "authorization_status": "authorized",
                "indexing_status": "on_demand",
                "last_indexed_at": None,
                "file_count": len(agents.list_project_files(workspace_id, workspaces)),
                "file_count_limit": agents.MAX_PROJECT_FILES,
                "search_available": True,
            }
            for workspace_id, path in sorted(workspaces.items())
        ],
    }


def _validate_project_text(values: list[str]) -> None:
    for value in values:
        try:
            agents._check_no_secret(value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


def _project_response(project: dict, workspaces: dict) -> dict:
    workspace = workspaces.get(project["workspace_id"])
    if workspace is None:
        raise HTTPException(status_code=409, detail="This project workspace is no longer authorized.")
    return {
        **project,
        "path": str(workspace),
        "authorization_status": "authorized",
        "search_available": True,
    }


@app.post("/api/projects", status_code=status.HTTP_201_CREATED)
def create_project(request: ProjectCreateRequest) -> dict:
    workspaces = settings.authorized_project_dirs
    if request.workspace_id not in workspaces:
        raise HTTPException(status_code=403, detail="Choose a configured, authorized workspace.")
    _validate_project_text([request.name, request.description, *request.tech_stack])
    if request.github_url:
        parsed_github_url = urlsplit(request.github_url)
        if (
            parsed_github_url.scheme != "https"
            or parsed_github_url.hostname not in {"github.com", "www.github.com"}
            or parsed_github_url.username is not None
            or parsed_github_url.password is not None
            or not parsed_github_url.path.strip("/")
        ):
            raise HTTPException(status_code=422, detail="Enter an HTTPS GitHub repository URL.")
    project_id = str(uuid4())
    with database.session() as connection:
        project = database.create_project(
            connection,
            project_id=project_id,
            name=request.name,
            description=request.description,
            workspace_id=request.workspace_id,
            status=request.status,
            priority=request.priority,
            progress=request.progress,
            start_date=request.start_date,
            target_date=request.target_date,
            tech_stack=request.tech_stack,
            github_url=request.github_url,
        )
    return _project_response(project, workspaces)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    with database.session() as connection:
        project = database.get_project(connection, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return _project_response(project, settings.authorized_project_dirs)


@app.put("/api/projects/{project_id}")
def edit_project(project_id: str, request: ProjectUpdateRequest) -> dict:
    fields = request.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="Provide at least one project field to update.")
    for key in ("name", "description", "status", "priority", "progress", "tech_stack"):
        if key in fields and fields[key] is None:
            raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').capitalize()} cannot be empty.")
    _validate_project_text([value for value in fields.values() if isinstance(value, str)])
    if fields.get("github_url"):
        parsed_github_url = urlsplit(fields["github_url"])
        if (
            parsed_github_url.scheme != "https"
            or parsed_github_url.hostname not in {"github.com", "www.github.com"}
            or parsed_github_url.username is not None
            or parsed_github_url.password is not None
            or not parsed_github_url.path.strip("/")
        ):
            raise HTTPException(status_code=422, detail="Enter an HTTPS GitHub repository URL.")
    with database.session() as connection:
        project = database.update_project(connection, project_id, fields)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return _project_response(project, settings.authorized_project_dirs)


@app.delete("/api/projects/{project_id}")
def remove_project(project_id: str) -> dict[str, str]:
    with database.session() as connection:
        project = database.get_project(connection, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        if project_id == project["workspace_id"]:
            database.update_project(connection, project_id, {"status": "archived"})
            return {"id": project_id, "status": "archived"}
        if not database.delete_project(connection, project_id):
            raise HTTPException(
                status_code=409,
                detail="This project has history or knowledge. Archive it to preserve linked tasks, notes, and memories.",
            )
    return {"id": project_id, "status": "deleted"}


@app.post("/api/projects/{project_id}/search")
def search_project(project_id: str, request: ProjectSearchRequest) -> list[dict[str, str]]:
    try:
        roots = _authorized_project_roots()
        return agents.inspect_project(
            project_id,
            request.query,
            roots,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/files")
def project_files(
    project_id: str,
    name: str = Query(default="", max_length=120),
    extension: str = Query(default="", max_length=12),
    folder: str = Query(default="", max_length=240),
) -> list[dict]:
    try:
        return agents.list_project_files(
            project_id,
            _authorized_project_roots(),
            name=name,
            extension=extension,
            folder=folder,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "authorized" in str(exc) else 422, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/files/content")
def project_file_content(project_id: str, path: str = Query(min_length=1, max_length=500)) -> dict:
    try:
        return agents.inspect_project_file(project_id, path, _authorized_project_roots())
    except ValueError as exc:
        raise HTTPException(status_code=404 if "authorized" in str(exc) else 422, detail=str(exc)) from exc


def _require_project(connection, project_id: str) -> dict:
    project = database.get_project(connection, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if project["workspace_id"] not in settings.authorized_project_dirs:
        raise HTTPException(status_code=409, detail="This project workspace is no longer authorized.")
    return project


@app.get("/api/projects/{project_id}/milestones")
def project_milestones(project_id: str) -> list[dict]:
    with database.session() as connection:
        _require_project(connection, project_id)
        return database.list_project_milestones(connection, project_id)


@app.post("/api/projects/{project_id}/milestones", status_code=status.HTTP_201_CREATED)
def create_project_milestone(project_id: str, request: ProjectMilestoneRequest) -> dict:
    _validate_project_text([request.title, request.description])
    with database.session() as connection:
        _require_project(connection, project_id)
        return database.save_project_milestone(
            connection, project_id, request.title, request.description, request.target_date, request.status
        )


@app.put("/api/projects/{project_id}/milestones/{milestone_id}")
def edit_project_milestone(project_id: str, milestone_id: str, request: ProjectMilestoneRequest) -> dict:
    _validate_project_text([request.title, request.description])
    with database.session() as connection:
        _require_project(connection, project_id)
        milestone = database.save_project_milestone(
            connection, project_id, request.title, request.description, request.target_date, request.status, milestone_id
        )
    if milestone is None:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    return milestone


@app.delete("/api/projects/{project_id}/milestones/{milestone_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project_milestone(project_id: str, milestone_id: str) -> Response:
    with database.session() as connection:
        _require_project(connection, project_id)
        if not database.delete_project_milestone(connection, project_id, milestone_id):
            raise HTTPException(status_code=404, detail="Milestone not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/projects/{project_id}/notes")
def project_notes(project_id: str) -> list[dict]:
    with database.session() as connection:
        _require_project(connection, project_id)
        return database.list_project_notes(connection, project_id)


@app.post("/api/projects/{project_id}/notes", status_code=status.HTTP_201_CREATED)
def create_project_note(project_id: str, request: ProjectNoteRequest) -> dict:
    _validate_project_text([request.title, request.content])
    with database.session() as connection:
        _require_project(connection, project_id)
        return database.save_project_note(
            connection, project_id, request.title, request.content, request.category
        )


@app.put("/api/projects/{project_id}/notes/{note_id}")
def edit_project_note(project_id: str, note_id: str, request: ProjectNoteRequest) -> dict:
    _validate_project_text([request.title, request.content])
    with database.session() as connection:
        _require_project(connection, project_id)
        note = database.save_project_note(
            connection, project_id, request.title, request.content, request.category, note_id
        )
    if note is None:
        raise HTTPException(status_code=404, detail="Project note not found.")
    return note


@app.delete("/api/projects/{project_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project_note(project_id: str, note_id: str) -> Response:
    with database.session() as connection:
        _require_project(connection, project_id)
        if not database.delete_project_note(connection, project_id, note_id):
            raise HTTPException(status_code=404, detail="Project note not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/research/status")
def research_status() -> dict[str, bool]:
    try:
        research_settings = ResearchSettings.from_env()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"configured": bool(research_settings.tavily_api_key)}


@app.post("/api/research")
async def research(request: ResearchRequest) -> dict:
    try:
        service = WebResearchService(ResearchSettings.from_env())
        response = await service.research(
            request.query,
            limit=request.limit,
            extract_sources=request.extract_sources,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return response.model_dump(mode="json")


@app.get("/api/agent-executions")
def agent_executions(limit: int = Query(default=50, ge=1, le=100)) -> list[dict]:
    with database.session() as connection:
        return database.list_agent_executions(connection, limit)


@app.get("/api/tasks")
def get_tasks(
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    project_id: str | None = Query(default=None, max_length=80),
    lifecycle: str | None = Query(default=None, max_length=24),
    include_archived: bool = Query(default=False),
) -> list[dict]:
    if status_filter and status_filter not in {"pending", "in_progress", "blocked", "done"}:
        raise HTTPException(status_code=422, detail="Unsupported task status filter.")
    if lifecycle and lifecycle not in database.TASK_LIFECYCLES:
        raise HTTPException(status_code=422, detail="Unsupported task lifecycle filter.")
    with database.session() as connection:
        if project_id:
            _require_project(connection, project_id)
        return database.list_tasks(
            connection,
            status_filter,
            project_id,
            lifecycle,
            include_archived=include_archived,
        )


def _validate_project_task_links(connection, project_id: str | None, milestone_id: str | None) -> None:
    if project_id:
        _require_project(connection, project_id)
    if milestone_id:
        row = connection.execute(
            "SELECT project_id FROM project_milestones WHERE id = ?",
            (milestone_id,),
        ).fetchone()
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=422, detail="Choose a milestone from the selected project.")


@app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
def post_task(request: TaskCreateRequest) -> dict:
    try:
        agents._check_no_secret(request.title)
        agents._check_no_secret(request.description)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with database.session() as connection:
        _validate_project_task_links(connection, request.project_id, request.milestone_id)
        task = database.create_task(
            connection,
            request.title,
            request.description,
            due_at=request.due_at,
            reminder_preference=request.reminder_preference,
            priority=request.priority,
            project_id=request.project_id,
            milestone_id=request.milestone_id,
        )
        reminders.schedule_task(connection, task)
        return task


@app.put("/api/tasks/{task_id}")
def edit_task(task_id: str, request: TaskUpdateRequest) -> dict:
    try:
        agents._check_no_secret(request.title)
        agents._check_no_secret(request.description)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with database.session() as connection:
        _validate_project_task_links(connection, request.project_id, request.milestone_id)
        task = database.update_task(
            connection,
            task_id,
            title=request.title,
            description=request.description,
            due_at=request.due_at,
            reminder_preference=request.reminder_preference,
            priority=request.priority,
            project_id=request.project_id,
            milestone_id=request.milestone_id,
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Active task not found.")
        reminders.schedule_task(connection, task)
        return task


@app.post("/api/tasks/{task_id}/status")
def update_task(task_id: str, request: TaskStatusRequest) -> dict:
    with database.session() as connection:
        task = database.update_task_status(connection, task_id, request.status)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        return task


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: str) -> dict:
    with database.session() as connection:
        task = database.complete_task(connection, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Active task not found.")
        return task


@app.delete("/api/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_task(task_id: str) -> Response:
    with database.session() as connection:
        if not database.delete_task(connection, task_id):
            raise HTTPException(status_code=404, detail="Task not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/tasks/{task_id}/confirm")
def confirm_detected_task(task_id: str, request: TaskConfirmRequest | None = None) -> dict:
    """Confirms a task the intelligence engine detected from a conversation."""
    with database.session() as connection:
        project_id = request.project_id if request is not None else None
        if project_id:
            _require_project(connection, project_id)
        task = database.confirm_task(connection, task_id, project_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Active task not found.")
        return task


@app.post("/api/tasks/{task_id}/archive")
def archive_task(task_id: str) -> dict:
    """Archives a task without deleting its record or its conversation evidence."""
    with database.session() as connection:
        task = database.archive_task(connection, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Active task not found.")
        return task


@app.post("/api/intelligence/scan")
async def intelligence_scan(request: IntelligenceScanRequest) -> dict:
    """Processes only the conversation messages past their stored checkpoints."""
    try:
        with database.session() as connection:
            return await intelligence.scan_conversations(
                connection,
                conversation_id=request.conversation_id,
                max_messages=request.max_messages,
                mode=request.mode,
                use_model=request.use_model,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="The intelligence scan could not use the local database.") from exc


@app.get("/api/intelligence/status")
def intelligence_status() -> dict:
    with database.session() as connection:
        return intelligence.status_snapshot(connection)


@app.get("/api/intelligence/runs")
def intelligence_runs(limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    with database.session() as connection:
        return database.list_intelligence_runs(connection, limit)


@app.get("/api/intelligence/proposals")
def intelligence_proposals(
    kind: str | None = Query(default=None, max_length=32),
    state: str | None = Query(default="pending", pattern="^(pending|accepted|rejected|all)$"),
    project_id: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    if kind and kind not in intelligence.PROPOSAL_KINDS:
        raise HTTPException(status_code=422, detail="Unsupported proposal kind.")
    with database.session() as connection:
        if project_id:
            _require_project(connection, project_id)
        return database.list_intelligence_proposals(
            connection,
            kind,
            None if state == "all" else state,
            project_id,
            limit,
        )


@app.post("/api/intelligence/proposals/{proposal_id}/review")
def review_intelligence_proposal(proposal_id: str, request: IntelligenceReviewRequest) -> dict:
    with database.session() as connection:
        try:
            return intelligence.review_proposal(
                connection,
                proposal_id,
                approve=request.approve,
                workspaces=settings.authorized_project_dirs,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            message = str(exc)
            status_code = 409 if "already reviewed" in message else 422
            raise HTTPException(status_code=status_code, detail=message) from exc


@app.get("/api/intelligence/summary")
def intelligence_summary(project_id: str | None = Query(default=None, max_length=80)) -> dict:
    with database.session() as connection:
        if project_id:
            _require_project(connection, project_id)
        try:
            return intelligence.build_summary(connection, project_id=project_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/intelligence/projects/{project_id}/aliases")
def project_aliases(project_id: str) -> list[dict]:
    with database.session() as connection:
        _require_project(connection, project_id)
        return database.list_project_aliases(connection, project_id)


@app.post("/api/intelligence/projects/{project_id}/aliases", status_code=status.HTTP_201_CREATED)
def create_project_alias(project_id: str, request: ProjectAliasRequest) -> dict:
    try:
        agents._check_no_secret(request.alias)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with database.session() as connection:
        _require_project(connection, project_id)
        alias = database.add_project_alias(connection, project_id, request.alias, source="manual")
        if alias is None:
            raise HTTPException(
                status_code=409,
                detail="That alias is invalid or already belongs to another project.",
            )
        return alias


@app.delete("/api/intelligence/projects/{project_id}/aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project_alias(project_id: str, alias_id: str) -> Response:
    with database.session() as connection:
        _require_project(connection, project_id)
        if not database.delete_project_alias(connection, project_id, alias_id):
            raise HTTPException(status_code=404, detail="Project alias not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/export")
def export_personal_data(user: dict = Depends(_session_user)) -> Response:
    """Exports this owner's stored personal data as JSON, excluding credentials."""
    with database.session() as connection:
        profile = database.get_owner_profile(connection)
        conversations = database.list_conversations(connection)
        conversation_bundles = [
            {
                "conversation": conversation,
                "messages": database.get_messages(connection, conversation["id"]),
            }
            for conversation in conversations
        ]
        project_bundles = [
            {
                "project": project,
                "milestones": database.list_project_milestones(connection, project["id"]),
                "notes": database.list_project_notes(connection, project["id"]),
            }
            for project in database.list_projects(connection)
        ]
        payload = {
            "export": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "format_version": 1,
                "application": "R Matrix Personal AI Command Center",
                "owner": user["username"],
                "included": [
                    "owner profile fields",
                    "conversations and messages (including stored source citations)",
                    "projects, milestones and notes",
                    "tasks and task reminders",
                    "approved memories and memory proposals",
                    "action approval records",
                    "agent enablement settings",
                    "document metadata (not extracted document contents)",
                ],
                "excluded": [
                    "password hashes and salts",
                    "session tokens and CSRF tokens",
                    "provider API keys and SMTP credentials",
                    "uploaded document files and extracted document pages/chunks",
                    "device credentials and enrollment codes",
                ],
            },
            "profile": profile,
            "conversations": conversation_bundles,
            "projects": project_bundles,
            "tasks": database.list_tasks(connection, None, None),
            "memories": database.list_approved_memories(connection),
            "memory_proposals": database.list_memory_proposals(connection),
            "action_approvals": database.list_action_approvals(connection),
            "agent_settings": database.get_agent_settings(
                connection, [agent["id"] for agent in agents.AGENT_CATALOG]
            ),
            "documents": documents.list_documents(connection),
        }
    filename = f"r-matrix-export-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/settings/profile")
def get_owner_profile() -> dict:
    with database.session() as connection:
        return database.get_owner_profile(connection)


@app.put("/api/settings/profile")
def save_owner_profile(request: OwnerProfileUpdateRequest) -> dict:
    fields = request.model_dump()
    for value in fields.values():
        try:
            agents._check_no_secret(value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    with database.session() as connection:
        return database.update_owner_profile(connection, fields)


@app.get("/api/settings/telegram")
def get_telegram_settings() -> dict:
    with database.session() as connection:
        return telegram.connection_status(connection)


@app.post("/api/settings/telegram/pairing-code", status_code=status.HTTP_201_CREATED)
def create_telegram_pairing(user: dict = Depends(_csrf_user)) -> dict:
    """Issues a short-lived, single-use pairing code for the signed-in owner."""
    with database.session() as connection:
        return telegram.create_pairing_code(connection, user["username"])


@app.post("/api/settings/telegram/verify")
async def verify_telegram(user: dict = Depends(_csrf_user)) -> dict:
    """Calls getMe without ever returning the bot token."""
    if not settings.telegram_bot_token:
        raise HTTPException(
            status_code=503,
            detail="TELEGRAM_BOT_TOKEN is not configured in backend/.env.",
        )
    try:
        profile = await telegram.get_me()
    except telegram.TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with database.session() as connection:
        database.update_telegram_state(
            connection,
            {
                "bot_username": profile.get("username"),
                "last_error": None,
                "last_error_at": None,
            },
        )
        return telegram.connection_status(connection)


@app.post("/api/settings/telegram/disconnect")
async def disconnect_telegram(user: dict = Depends(_csrf_user)) -> dict:
    with database.session() as connection:
        return await telegram.disconnect(connection)


@app.get("/api/settings/reminders")
def get_reminder_settings() -> dict:
    with database.session() as connection:
        return reminders.reminder_status(connection)


@app.put("/api/settings/reminders")
def save_reminder_settings(request: ReminderRecipientRequest) -> dict:
    with database.session() as connection:
        database.set_reminder_recipient(connection, request.recipient_email)
        return reminders.reminder_status(connection)


@app.post("/api/settings/reminders/test")
async def test_reminder_email() -> dict[str, str]:
    with database.session() as connection:
        recipient = database.get_reminder_recipient(connection)
    if not recipient:
        raise HTTPException(status_code=422, detail="Set a reminder recipient email address first.")
    try:
        await reminders.send_test_email(recipient)
    except (RuntimeError, OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Test email failed ({type(exc).__name__}). Check SMTP settings in backend/.env.",
        ) from exc
    return {"status": "sent", "message": f"Test email sent to {recipient}."}


@app.get("/api/memories")
def get_memories(
    query: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=40),
    project_id: str | None = Query(default=None, max_length=80),
    include_archived: bool = False,
) -> list[dict]:
    with database.session() as connection:
        if project_id:
            _require_project(connection, project_id)
        return database.list_approved_memories(connection, query, category, project_id, include_archived)


@app.get("/api/memory-proposals")
def get_memory_proposals(
    query: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=40),
    project_id: str | None = Query(default=None, max_length=80),
    state: str | None = Query(default="pending", pattern="^(pending|approved|rejected)$"),
) -> list[dict]:
    with database.session() as connection:
        if project_id:
            _require_project(connection, project_id)
        return database.list_memory_proposals(connection, query, category, project_id, state)


@app.post("/api/memory-proposals", status_code=status.HTTP_201_CREATED)
def post_memory_proposal(request: MemoryProposalRequest) -> dict:
    try:
        agents._check_no_secret(request.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if request.project_id:
        with database.session() as connection:
            _require_project(connection, request.project_id)
    normalized = " ".join(request.content.casefold().split())
    memory_hash = hashlib.sha256(
        f"{request.project_id or 'global'}\n{normalized}".encode("utf-8")
    ).hexdigest()
    with database.session() as connection:
        return database.create_memory_proposal(
            connection,
            request.content,
            memory_hash,
            "Explicit user request from the local dashboard",
            category=request.category,
            project_id=request.project_id,
        )


@app.post("/api/memory-proposals/{proposal_id}/review")
def review_memory(proposal_id: str, request: MemoryReviewRequest) -> dict:
    with database.session() as connection:
        result = database.review_memory_proposal(connection, proposal_id, request.approve)
        if result is None:
            raise HTTPException(status_code=404, detail="Pending memory proposal not found.")
        return result


@app.put("/api/memories/{memory_id}")
def update_memory(memory_id: str, request: MemoryUpdateRequest) -> dict:
    try:
        agents._check_no_secret(request.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    normalized = " ".join(request.content.casefold().split())
    with database.session() as connection:
        current = connection.execute(
            "SELECT category, project_id FROM approved_memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Approved memory not found.")
        category = request.category if "category" in request.model_fields_set else current["category"]
        project_id = request.project_id if "project_id" in request.model_fields_set else current["project_id"]
        if project_id:
            _require_project(connection, project_id)
        memory_hash = hashlib.sha256(
            f"{project_id or 'global'}\n{normalized}".encode("utf-8")
        ).hexdigest()
        try:
            result = database.update_approved_memory(
                connection,
                memory_id,
                request.content,
                memory_hash,
                category=category,
                project_id=project_id,
            )
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise HTTPException(status_code=409, detail="A matching approved memory already exists.") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Approved memory not found.")
        return result


@app.delete("/api/memories", status_code=status.HTTP_200_OK)
def clear_memories(request: MemoryClearRequest) -> dict[str, int]:
    if not request.confirm:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required to clear memories.")
    with database.session() as connection:
        return {"deleted": database.clear_approved_memories(connection)}


@app.delete("/api/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_memory(memory_id: str) -> Response:
    with database.session() as connection:
        if not database.delete_approved_memory(connection, memory_id):
            raise HTTPException(status_code=404, detail="Approved memory not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/memories/{memory_id}/archive")
def archive_memory(memory_id: str) -> dict[str, str]:
    with database.session() as connection:
        if not database.archive_approved_memory(connection, memory_id):
            raise HTTPException(status_code=404, detail="Approved memory not found.")
    return {"id": memory_id, "status": "archived"}


@app.get("/api/action-approvals")
def action_approvals() -> list[dict]:
    with database.session() as connection:
        return database.list_action_approvals(connection)


@app.post("/api/action-approvals", status_code=status.HTTP_201_CREATED)
def post_action_approval(request: ApprovalProposalRequest) -> dict:
    try:
        agents._check_no_secret(request.summary)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with database.session() as connection:
        return database.create_action_approval(
            connection,
            request.operation,
            "Target and exact inputs require a separate implementation and review.",
            request.summary,
            {},
        )


@app.post("/api/action-approvals/{approval_id}/cancel")
def cancel_approval(approval_id: str) -> dict[str, str]:
    with database.session() as connection:
        if not database.cancel_action_approval(connection, approval_id):
            raise HTTPException(status_code=404, detail="Pending approval proposal not found.")
    return {"status": "cancelled"}
