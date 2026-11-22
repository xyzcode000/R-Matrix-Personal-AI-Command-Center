from typing import Literal

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic import BaseModel, ConfigDict, Field

SENSITIVE_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|secret|credential)\s*[:=]\s*\S+|"
    r"(?:api[_-]?key|access[_-]?token|password|secret|credential)\s+is\s+\S+|"
    r"bearer\s+[A-Za-z0-9._~+/-]+=*|AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9_-]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


class DocumentContextPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    filename: str
    document_name: str | None = None
    document_status: str | None = None
    original_filename: str | None = None
    category: str | None = None
    document_type: str | None = None
    page_number: int | None
    section_heading: str | None = None
    snippet: str
    excerpt: str | None = None
    relevance_score: float
    ocr_confidence: float | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=200)
    web_search: Literal["auto", "always", "never"] = "auto"
    allow_cloud_document_context: bool = False
    document_context_preview: list[DocumentContextPreview] | None = Field(default=None, max_length=4)
    project_id: str | None = Field(default=None, max_length=80)
    allow_cloud_project_context: bool = False


class ChatMessage(BaseModel):
    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    sources: list[dict] = Field(default_factory=list)
    created_at: str
    provider: str | None = None
    model: str | None = None
    execution: dict | None = None


class AgentExecution(BaseModel):
    id: str
    agent_id: str
    reason: str
    status: Literal["succeeded", "partial", "failed"]
    tools: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    approval_ids: list[str] = Field(default_factory=list)
    model_used: bool = False
    steps: int = Field(default=0, ge=0, le=2)
    project_id: str | None = None
    answer_state: str | None = None


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(default="", max_length=1000)
    due_at: str | None = Field(default=None, max_length=40)
    reminder_preference: Literal["none", "at_due", "15_minutes", "1_hour", "1_day"] = "none"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    project_id: str | None = Field(default=None, max_length=80)
    milestone_id: str | None = Field(default=None, max_length=64)

    @field_validator("due_at")
    @classmethod
    def normalize_due_at(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Due date must be a valid ISO 8601 date-time.") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return parsed.astimezone(timezone.utc).isoformat()


class TaskUpdateRequest(TaskCreateRequest):
    pass


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    workspace_id: str = Field(min_length=1, max_length=120)
    status: Literal["active", "planned", "paused", "completed", "archived"] = "active"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    progress: int = Field(default=0, ge=0, le=100)
    start_date: str | None = Field(default=None, max_length=10)
    target_date: str | None = Field(default=None, max_length=10)
    tech_stack: list[str] = Field(default_factory=list, max_length=20)
    github_url: str | None = Field(default=None, max_length=500)


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "planned", "paused", "completed", "archived"] | None = None
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    start_date: str | None = Field(default=None, max_length=10)
    target_date: str | None = Field(default=None, max_length=10)
    tech_stack: list[str] | None = Field(default=None, max_length=20)
    github_url: str | None = Field(default=None, max_length=500)


class IntelligenceScanRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    conversation_id: str | None = Field(default=None, max_length=64)
    mode: Literal["incremental", "reprocess"] = "incremental"
    max_messages: int | None = Field(default=None, ge=1, le=5000)
    use_model: bool = False


class IntelligenceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


class TaskConfirmRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    project_id: str | None = Field(default=None, max_length=80)


class ProjectAliasRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    alias: str = Field(min_length=2, max_length=80)


class ProjectMilestoneRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    target_date: str | None = Field(default=None, max_length=10)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class ProjectNoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=5000)
    category: Literal[
        "architecture", "decision", "development", "requirement", "bug", "implementation", "idea"
    ] = "development"


class OwnerProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    full_name: str = Field(min_length=1, max_length=120)
    preferred_name: str = Field(min_length=1, max_length=80)
    user_role: str = Field(min_length=1, max_length=120)
    personal_context: str = Field(default="", max_length=2000)
    project_context: str = Field(default="", max_length=2000)
    current_priorities: str = Field(default="", max_length=2000)


class ReminderRecipientRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    recipient_email: str = Field(max_length=254)

    @field_validator("recipient_email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid recipient email address.")
        return value


class AgentEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class TaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "in_progress", "blocked"]


class MemoryProposalRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(min_length=5, max_length=500)
    category: Literal[
        "owner_profile", "preference", "project", "technical_decision", "context", "goal", "work_history"
    ] = "context"
    project_id: str | None = Field(default=None, max_length=80)


class MemoryUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(min_length=5, max_length=500)
    category: Literal[
        "owner_profile", "preference", "project", "technical_decision", "context", "goal", "work_history"
    ] | None = None
    project_id: str | None = Field(default=None, max_length=80)


class MemoryReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


class MemoryClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool


class ApprovalProposalRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    operation: Literal["file.write", "external.communication", "security.change"]
    summary: str = Field(min_length=5, max_length=300)


class ChatResponse(BaseModel):
    conversation_id: str
    user_message: ChatMessage
    message: ChatMessage
    provider: str | None
    model: str | None
    document_context_included: bool
    execution: AgentExecution | None = None


class ProviderInfo(BaseModel):
    id: str
    name: str
    cloud: bool
    configured: bool
    selected: bool
    model: str | None = None
    configuration_error: str | None = None
    connection_verified: bool = False


class DocumentSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=10, ge=1, le=25)
    document_ids: list[str] | None = Field(default=None, max_length=25)
    wide: bool = False


class DocumentMetadataUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=600)
    category: str = Field(default="Other", max_length=32)


class DocumentBulkDeleteRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=200)
