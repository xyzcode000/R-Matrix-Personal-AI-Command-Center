"""Incremental personal intelligence engine for R Matrix.

The engine reads the conversations this application already stores and the projects,
milestones, notes and tasks that already exist. It never replaces them: every detected
fact is written into the existing task/project tables or into reviewable proposals, and
every fact keeps the conversation, message and excerpt it came from.

Grounding rules (no invented facts):

* Only user-authored messages create facts. Assistant text is never evidence, so a model
  claim such as "progress is 90%" cannot become stored progress or a completed task.
* A deadline exists only when the message itself contains a date phrase.
* Progress exists only as an explicit percentage written by the user, and is proposed for
  review instead of being applied automatically.
* Completion needs an explicit completion statement plus a matching open task, and is
  applied only after the owner accepts the proposal.
* Anything uncertain stays unconfirmed: detected tasks are created with
  ``confirmation_status = 'pending'`` and are never treated as confirmed work.

Telegram can feed the same engine later by calling :func:`analyze_message` with the
message content, role and timestamp; nothing in the extraction depends on the chat UI.
"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import database, ollama, time_context
from .config import settings

logger = logging.getLogger(__name__)

MAX_TASK_TITLE = 180
MAX_EXCERPT = 400
MATCH_THRESHOLD = 0.7
AMBIGUITY_MARGIN = 0.15
UPCOMING_WINDOW_DAYS = 14
STALE_PROJECT_DAYS = 30
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
PROPOSAL_KINDS = (
    "project",
    "task",
    "task_completion",
    "project_progress",
    "blocker",
    "milestone",
)

_NAME_STOP_WORDS = {
    "the", "a", "an", "of", "and", "for", "to", "in", "on", "with", "my", "our",
    "this", "that", "new", "project", "projects",
}
_GENERIC_PROJECT_TOKENS = {
    "project", "projects", "app", "apps", "system", "systems", "platform", "platforms",
    "dashboard", "website", "site", "tool", "tools", "bot", "service", "services",
    "work", "update", "updates", "task", "tasks", "thing", "things", "stuff",
}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|(?<=;)\s+", re.UNICODE)
_MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b")
_DAY_MONTH_YEAR = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_PATTERN})\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
_RELATIVE_DATE = re.compile(
    r"\b(?:today|tomorrow|tonight|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|in\s+\d{1,4}\s+days?|next\s+week|end\s+of\s+(?:this|the current)\s+month)\b",
    re.IGNORECASE,
)
_DEADLINE_CUE = re.compile(
    r"\b(?:by|before|due(?:\s+on)?|deadline(?:\s+is)?|until|no\s+later\s+than)\b[^.;\n!?]{0,40}",
    re.IGNORECASE,
)
_BARE_WEEKDAY = re.compile(
    r"\b(?:by|before|due(?:\s+on)?|until|no\s+later\s+than)\s+"
    r"(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_WEEKDAYS = {
    name: index
    for index, name in enumerate(
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    )
}
_TASK_PATTERNS = (
    (re.compile(r"\b(?:i|we)\s+(?:really\s+)?(?:need|have|got)\s+to\s+(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.62),
    (re.compile(r"\b(?:i|we)\s+must\s+(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.62),
    (re.compile(r"\b(?:i|we)\s+should\s+(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.55),
    (re.compile(r"\b(?:remind\s+me|remember)\s+to\s+(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.72),
    (re.compile(r"\b(?:don'?t|do\s+not)\s+forget\s+to\s+(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.72),
    (
        re.compile(
            r"\b(?:add|create|track|log)\s+(?:a\s+|an\s+|the\s+)?(?:task|todo|to-do|ticket)\s*[:\-]?\s*(?P<body>[^.;\n!?]+)",
            re.IGNORECASE,
        ),
        0.8,
    ),
    (re.compile(r"\b(?:todo|to-do|task)\s*[:\-]\s*(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.8),
    (re.compile(r"\bnext\s+steps?\s*(?:is|are)?\s*[:\-]?\s*(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.7),
)
_PROJECT_PATTERNS = (
    (
        re.compile(
            r"\b(?:i\s+am|i'm|we\s+are|we're)\s+(?:currently\s+)?"
            r"(?:building|developing|working\s+on|launching|maintaining|creating)\s+(?P<body>[^.;\n!?]+)",
            re.IGNORECASE,
        ),
        0.8,
    ),
    (
        re.compile(
            r"\b(?:my|our)\s+(?:new\s+)?project\s+(?:is\s+called|is\s+named|called|named|is)\s+(?P<body>[^.;\n!?]+)",
            re.IGNORECASE,
        ),
        0.8,
    ),
    (re.compile(r"\bproject\s*[:\-]\s*(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.7),
)
_MILESTONE_PATTERNS = (
    (re.compile(r"\bmilestones?\s*[:\-]\s*(?P<body>[^.;\n!?]+)", re.IGNORECASE), 0.8),
    (
        re.compile(r"\b(?:next|first|final)\s+milestone\s+(?:is|will\s+be)\s+(?P<body>[^.;\n!?]+)", re.IGNORECASE),
        0.75,
    ),
)
_BLOCKER_PATTERN = re.compile(
    r"\b(?:blocked\s+(?:by|on)|waiting\s+(?:on|for)|stuck\s+on|"
    r"can'?t\s+(?:proceed|continue|move\s+forward)\s+(?:until|without))\s+(?P<body>[^.;\n!?]+)",
    re.IGNORECASE,
)
_COMPLETION_PATTERN = re.compile(
    r"\b(?:(?:i|we)\s+(?:have\s+|just\s+)?)?"
    r"(?:finished|completed|shipped|deployed|merged|closed|done\s+with)\s+"
    r"(?:the\s+|my\s+|our\s+)?(?P<body>[^.;\n!?]+)",
    re.IGNORECASE,
)
_PROGRESS_PATTERN = re.compile(
    r"\b(?:(?P<before>progress|done|complete|completed|finished)\D{0,12}(?P<value_before>\d{1,3})\s*%"
    r"|(?P<value_after>\d{1,3})\s*%\s*(?:done|complete|completed|through|finished|progress))",
    re.IGNORECASE,
)
_PRIORITY_URGENT = re.compile(r"\b(?:urgent|asap|as\s+soon\s+as\s+possible|critical|blocker)\b", re.IGNORECASE)
_PRIORITY_HIGH = re.compile(r"\b(?:high\s+priority|important|priority)\b", re.IGNORECASE)
_PRIORITY_LOW = re.compile(
    r"\b(?:low\s+priority|whenever|when\s+you\s+get\s+a\s+chance|no\s+rush|someday|later)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    workspace_id: str
    description: str
    tech_stack: tuple[str, ...]
    status: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class DueInfo:
    due_at: str
    phrase: str
    precision: str


@dataclass
class ExtractedTask:
    title: str
    detail: str
    due: DueInfo | None
    priority: str
    priority_explicit: bool
    project_id: str | None
    project_name: str | None
    project_score: float
    confidence: float
    note: str
    sentence: str
    fingerprint: str


def _now() -> str:
    return database.utc_now()


def _message_time(value: str | None, fallback: datetime | None = None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ZoneInfo(time_context.DEFAULT_TIMEZONE))
        except ValueError:
            pass
    return time_context.current_time(fallback)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text or "") if part and part.strip()]


def _name_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").casefold())
        if token not in _NAME_STOP_WORDS
    ]


def _content_tokens(text: str) -> set[str]:
    return {token for token in _name_tokens(text) if len(token) > 2}


def _end_of_day(target: date, reference: datetime) -> datetime:
    return datetime.combine(target, time(23, 59), tzinfo=reference.tzinfo)


def _to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _excerpt(text: str) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:MAX_EXCERPT]


# ---------------------------------------------------------------------------
# Project matching
# ---------------------------------------------------------------------------


def project_index(connection: sqlite3.Connection) -> list[ProjectRecord]:
    """Builds the matching index from the existing project records and aliases."""
    aliases = database.list_all_project_aliases(connection)
    records: list[ProjectRecord] = []
    for project in database.list_projects(connection):
        if project["status"] == "archived":
            continue
        records.append(
            ProjectRecord(
                id=project["id"],
                name=project["name"],
                workspace_id=project["workspace_id"],
                description=project["description"],
                tech_stack=tuple(project["tech_stack"]),
                status=project["status"],
                aliases=tuple(aliases.get(project["id"], [])),
            )
        )
    return records


def _longest_common_run(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    best = 0
    previous = [0] * (len(right) + 1)
    for left_token in left:
        current = [0] * (len(right) + 1)
        for index, right_token in enumerate(right, start=1):
            if left_token == right_token:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def _variant_score(folded_text: str, text_tokens: set[str], variant: str) -> float:
    """Scores how strongly a project name or alias is mentioned in the text.

    Full phrases and multi-word name fragments score highest; a single distinctive word
    ("Rocket", "Garden") still matches so abbreviated mentions resolve, while the
    ambiguity check keeps similarly named projects apart.
    """
    variant_normalized = " ".join((variant or "").casefold().split())
    if len(variant_normalized) < 3:
        return 0.0
    if re.search(rf"(?<![a-z0-9]){re.escape(variant_normalized)}(?![a-z0-9])", folded_text):
        return 1.0
    variant_tokens = [token for token in _name_tokens(variant_normalized) if len(token) > 1]
    if not variant_tokens:
        return 0.0
    text_sequence = _name_tokens(folded_text)
    if _longest_common_run(text_sequence, variant_tokens) >= 2 and any(
        len(token) >= 3 and token in text_tokens for token in variant_tokens
    ):
        return 0.9
    significant = [token for token in variant_tokens if len(token) > 2]
    if len(significant) >= 2 and all(token in text_tokens for token in significant):
        return 0.8
    distinctive = [
        token for token in variant_tokens
        if len(token) >= 4 and token in text_tokens and token not in _GENERIC_PROJECT_TOKENS
    ]
    if distinctive:
        return 0.7
    return 0.0


def score_projects(text: str, projects: list[ProjectRecord]) -> list[tuple[float, ProjectRecord]]:
    folded = " ".join((text or "").casefold().split())
    text_tokens = _content_tokens(text)
    scored: list[tuple[float, ProjectRecord]] = []
    for project in projects:
        workspace_variant = project.workspace_id.replace("-", " ").replace("_", " ")
        variants = [project.name, workspace_variant, *project.aliases]
        best = max((_variant_score(folded, text_tokens, variant) for variant in variants), default=0.0)
        scored.append((best, project))
    # Prefer an explicitly created project over the auto-generated workspace placeholder.
    scored.sort(key=lambda item: (-item[0], 0 if item[1].id != item[1].workspace_id else 1))
    return scored


def match_project(
    text: str,
    projects: list[ProjectRecord],
) -> tuple[ProjectRecord | None, float, bool]:
    """Returns the best project, its score, and whether several projects are plausible."""
    scored = score_projects(text, projects)
    if not scored or scored[0][0] < MATCH_THRESHOLD:
        return None, scored[0][0] if scored else 0.0, False
    # Records whose names share the same words describe the same project lineage (for
    # example the workspace placeholder and the project created for that workspace), so
    # they are not treated as unrelated projects competing for the mention.
    distinct: list[tuple[float, ProjectRecord]] = []
    signatures: list[frozenset[str]] = []
    for score, project in scored:
        if score < MATCH_THRESHOLD:
            break
        signature = frozenset(_content_tokens(project.name) or {project.name.casefold()})
        if any(signature == existing for existing in signatures):
            continue
        signatures.append(signature)
        distinct.append((score, project))
    best_score, best_project = distinct[0]
    ambiguous = any(
        best_score - score < AMBIGUITY_MARGIN for score, _ in distinct[1:]
    )
    return best_project, best_score, ambiguous


def match_candidate_name(
    candidate: str,
    projects: list[ProjectRecord],
) -> tuple[ProjectRecord | None, float]:
    """Matches a discovered project name against existing projects to avoid duplicates."""
    candidate_tokens = set(_content_tokens(candidate))
    if not candidate_tokens:
        return None, 0.0
    best: tuple[ProjectRecord | None, float] = (None, 0.0)
    for project in projects:
        variants = [project.name, project.workspace_id.replace("-", " "), *project.aliases]
        for variant in variants:
            variant_tokens = set(_content_tokens(variant))
            if not variant_tokens:
                continue
            shared = candidate_tokens & variant_tokens
            if not shared:
                continue
            coverage_candidate = len(shared) / len(candidate_tokens)
            coverage_variant = len(shared) / len(variant_tokens)
            score = 0.0
            if len(shared) >= 2 and (candidate_tokens <= variant_tokens or variant_tokens <= candidate_tokens):
                score = 0.95
            elif coverage_candidate >= 0.8:
                score = 0.85
            elif coverage_variant >= 0.8 and len(shared) >= 1:
                score = 0.8
            elif coverage_candidate >= 0.5:
                score = 0.55
            if score > best[1]:
                best = (project, score)
    if best[1] < MATCH_THRESHOLD:
        return None, best[1]
    return best


# ---------------------------------------------------------------------------
# Date, priority and text extraction
# ---------------------------------------------------------------------------


def _explicit_date(text: str) -> tuple[date, str] | None:
    match = _ISO_DATE.search(text)
    if match:
        try:
            return date(int(match.group("year")), int(match.group("month")), int(match.group("day"))), match.group(0)
        except ValueError:
            pass
    for pattern in (_DAY_MONTH_YEAR, _MONTH_DAY_YEAR):
        match = pattern.search(text)
        if match:
            parsed = time_context.parse_explicit_date(match.group(0))
            if parsed is not None:
                return parsed, match.group(0)
    return None


def resolve_deadline(text: str, reference: datetime) -> DueInfo | None:
    """Finds a deadline only when the text itself contains a date phrase."""
    explicit = _explicit_date(text)
    if explicit is not None:
        target_date, phrase = explicit
        return DueInfo(_to_utc_iso(_end_of_day(target_date, reference)), phrase, "date")

    if re.search(r"\btonight\b", text, re.IGNORECASE):
        return DueInfo(_to_utc_iso(_end_of_day(reference.date(), reference)), "tonight", "date")

    resolved = time_context.resolve_relative_dates(text, now=reference)
    if resolved:
        item = next((entry for entry in resolved if entry.expression.casefold() != "yesterday"), None)
        if item is not None:
            end = item.end or datetime.combine(item.start.date(), time(23, 59), tzinfo=item.start.tzinfo)
            precision = "range" if item.end is not None else "date"
            return DueInfo(_to_utc_iso(end), item.expression, precision)

    weekday = _BARE_WEEKDAY.search(text)
    if weekday is not None:
        target_index = _WEEKDAYS[weekday.group("weekday").casefold()]
        offset = (target_index - reference.weekday()) % 7
        target_date = reference.date() + timedelta(days=offset)
        return DueInfo(
            _to_utc_iso(_end_of_day(target_date, reference)),
            weekday.group(0),
            "date",
        )
    return None


def detect_priority(text: str) -> tuple[str, bool]:
    if _PRIORITY_URGENT.search(text):
        return "urgent", True
    if _PRIORITY_LOW.search(text):
        return "low", True
    if _PRIORITY_HIGH.search(text):
        return "high", True
    return "medium", False


def _clean_title(body: str, due: DueInfo | None = None) -> str:
    title = " ".join((body or "").split())
    title = re.sub(r"^(?:the|a|an|my|our)\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(?:please|thanks|thank\s+you)\b", " ", title, flags=re.IGNORECASE)
    if due is not None:
        title = re.sub(
            r"\b(?:by|before|due(?:\s+on)?|deadline(?:\s+is)?|until|no\s+later\s+than)\b\s*"
            + re.escape(due.phrase),
            " ",
            title,
            flags=re.IGNORECASE,
        )
        title = title.replace(due.phrase, " ")
    title = re.split(
        r"\s*[,;]\s*(?:this|that|it)\s+is\b|\s+(?:which|since)\s+",
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    title = re.sub(r"[\s,;:.\-]+$", "", title)
    title = " ".join(title.split())
    if title:
        title = title[0].upper() + title[1:]
    if len(title) > MAX_TASK_TITLE:
        title = title[:MAX_TASK_TITLE].rsplit(" ", 1)[0].rstrip(",;:-")
    return title


def _valid_title(title: str) -> bool:
    if len(title) < 4:
        return False
    if not re.search(r"[A-Za-z]{3,}", title):
        return False
    if re.match(r"^(?:be|have|been|is|are|was|were)\b", title, re.IGNORECASE):
        return False
    return not title.endswith("?")


def _fingerprint(*parts: str) -> str:
    normalized = "|".join(" ".join((part or "").casefold().split()) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _task_fingerprint(project_id: str | None, title: str, due_at: str | None) -> str:
    normalized_title = " ".join(re.findall(r"[a-z0-9]+", _clean_title(title).casefold()))
    due_key = (due_at or "")[:10]
    return _fingerprint("task", project_id or "", normalized_title, due_key)


def _title_similarity(left: str, right: str) -> float:
    left_tokens = _content_tokens(left)
    right_tokens = _content_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    if not shared:
        return 0.0
    union = left_tokens | right_tokens
    jaccard = len(shared) / len(union)
    coverage = len(shared) / min(len(left_tokens), len(right_tokens))
    return max(jaccard, coverage * 0.9)


def _project_note(project: ProjectRecord | None, score: float) -> str:
    if project is None:
        return "No existing project matched this task; confirm which project it belongs to."
    if score < MATCH_THRESHOLD:
        return f"Possible project: {project.name}. Confirm the project before relying on it."
    return ""


# ---------------------------------------------------------------------------
# Message analysis
# ---------------------------------------------------------------------------


def extract_facts(
    text: str,
    *,
    reference_time: datetime,
    projects: list[ProjectRecord],
) -> dict[str, list[Any]]:
    """Extracts grounded facts from one user message. Assistant text is never passed here."""
    facts: dict[str, list[Any]] = {
        "tasks": [],
        "projects": [],
        "progress": [],
        "blockers": [],
        "completions": [],
        "milestones": [],
    }
    if not text or not text.strip():
        return facts

    sentences = _sentences(text)
    # A project named in one sentence must not silently absorb unrelated work that is only
    # described in another sentence; a single-sentence message can still rely on its own text.
    allow_message_fallback = len(sentences) <= 1
    for sentence in sentences:
        due = resolve_deadline(sentence, reference_time)
        priority, priority_explicit = detect_priority(sentence)
        project, score, ambiguous = match_project(sentence, projects)
        if project is None and allow_message_fallback:
            project, score, ambiguous = match_project(text, projects)

        matched_task = False
        for pattern, weight in _TASK_PATTERNS:
            match = pattern.search(sentence)
            if match is None:
                continue
            title = _clean_title(match.group("body"), due)
            if not _valid_title(title):
                continue
            confidence = weight
            if due is not None:
                confidence += 0.12
            if project is not None and not ambiguous:
                confidence += 0.1
            if priority_explicit:
                confidence += 0.04
            note = _project_note(project, score)
            if ambiguous:
                note = "Several projects could match this task; confirm the correct project."
            facts["tasks"].append(
                ExtractedTask(
                    title=title,
                    detail=_excerpt(sentence),
                    due=due,
                    priority=priority,
                    priority_explicit=priority_explicit,
                    project_id=project.id if project is not None and not ambiguous else None,
                    project_name=project.name if project is not None else None,
                    project_score=score,
                    confidence=round(min(confidence, 0.95), 2),
                    note=note,
                    sentence=_excerpt(sentence),
                    fingerprint=_task_fingerprint(
                        project.id if project is not None and not ambiguous else None,
                        title,
                        due.due_at if due else None,
                    ),
                )
            )
            matched_task = True
            break
        if matched_task:
            continue

        for pattern, weight in _MILESTONE_PATTERNS:
            match = pattern.search(sentence)
            if match is None:
                continue
            title = _clean_title(match.group("body"), due)
            if not _valid_title(title):
                continue
            if project is None or ambiguous:
                continue
            facts["milestones"].append(
                {
                    "title": title,
                    "detail": _excerpt(sentence),
                    "due": due,
                    "project": project,
                    "confidence": round(min(weight + (0.1 if due else 0.0), 0.95), 2),
                }
            )
            break

        blocker = _BLOCKER_PATTERN.search(sentence)
        if blocker is not None:
            title = _clean_title(blocker.group("body"), None)
            if _valid_title(title):
                facts["blockers"].append(
                    {
                        "title": title,
                        "detail": _excerpt(sentence),
                        "project": project if not ambiguous else None,
                        "confidence": 0.7 if project is not None and not ambiguous else 0.5,
                    }
                )

        completion = _COMPLETION_PATTERN.search(sentence)
        if completion is not None:
            body = completion.group("body") or completion.group(0)
            title = _clean_title(body, None)
            if _valid_title(title):
                facts["completions"].append(
                    {
                        "title": title,
                        "detail": _excerpt(sentence),
                        "project": project if not ambiguous else None,
                        "confidence": 0.75 if project is not None and not ambiguous else 0.6,
                    }
                )

        progress = _PROGRESS_PATTERN.search(sentence)
        if progress is not None:
            raw_value = progress.group("value_before") or progress.group("value_after")
            try:
                percent = int(raw_value)
            except (TypeError, ValueError):
                percent = -1
            if 0 <= percent <= 100:
                facts["progress"].append(
                    {
                        "percent": percent,
                        "phrase": progress.group(0),
                        "detail": _excerpt(sentence),
                        "project": project if not ambiguous else None,
                        "confidence": 0.85 if project is not None and not ambiguous else 0.55,
                    }
                )

    for pattern, weight in _PROJECT_PATTERNS:
        for match in pattern.finditer(text):
            candidate = _clean_candidate_name(match.group("body"))
            if len(candidate) < 3:
                continue
            existing, score = match_candidate_name(candidate, projects)
            facts["projects"].append(
                {
                    "name": candidate,
                    "detail": _excerpt(match.group(0)),
                    "existing": existing,
                    "score": score,
                    "confidence": round(weight if existing is None else 0.4, 2),
                }
            )
            break
    return facts


def _clean_candidate_name(body: str) -> str:
    name = " ".join((body or "").split())
    name = re.split(
        r"\b(?:using|with|because|which|that|so\s+that|to\s+build|and\s+then)\b",
        name,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    name = re.sub(r"^(?:the|a|an|my|our)\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+(?:project|app|application|platform|system)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[\s,;:.\-]+$", "", name)
    name = " ".join(name.split())
    if len(name) > 80:
        name = name[:80].rsplit(" ", 1)[0]
    return name[0].upper() + name[1:] if name else name


def _evidence(conversation_id: str | None, message: dict[str, Any], sentence: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "message_id": message.get("id"),
        "message_created_at": message.get("created_at"),
        "excerpt": _excerpt(sentence),
    }


def _similar_existing_task(
    connection: sqlite3.Connection,
    project_id: str | None,
    title: str,
    due_at: str | None,
    fingerprint: str,
) -> dict[str, Any] | None:
    existing = database.list_tasks(connection, include_archived=True)
    for task in existing:
        if task.get("dedupe_hash") == fingerprint:
            return task
        if (task.get("project_id") or None) != (project_id or None):
            continue
        if _title_similarity(title, task["title"]) >= 0.8:
            return task
    return None


def _record_task(
    connection: sqlite3.Connection,
    task: ExtractedTask,
    *,
    conversation_id: str,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    existing = _similar_existing_task(
        connection, task.project_id, task.title, task.due.due_at if task.due else None, task.fingerprint
    )
    if existing is not None:
        return None
    return database.create_task(
        connection,
        task.title,
        task.detail,
        conversation_id=conversation_id,
        due_at=task.due.due_at if task.due else None,
        reminder_preference="none",
        priority=task.priority,
        project_id=task.project_id,
        origin="conversation",
        confirmation_status="pending",
        confirmation_note=task.note or None,
        confidence=task.confidence,
        source_message_id=message.get("id"),
        source_excerpt=task.sentence,
        dedupe_hash=task.fingerprint,
        due_precision=task.due.precision if task.due else None,
        detected_at=_now(),
    )


def _completion_candidate(
    connection: sqlite3.Connection,
    title: str,
    project_id: str | None,
) -> dict[str, Any] | None:
    """Finds the single open task that an explicit completion statement refers to."""
    candidates = []
    for task in database.list_tasks(connection):
        if task["status"] == "done":
            continue
        if project_id and task["project_id"] and task["project_id"] != project_id:
            continue
        coverage = _completion_coverage(title, task["title"])
        if coverage >= 0.6:
            candidates.append((coverage, task))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 0.05:
        return None
    return candidates[0][1]


def _completion_coverage(statement: str, task_title: str) -> float:
    statement_tokens = _content_tokens(statement)
    task_tokens = _content_tokens(task_title)
    if not statement_tokens or not task_tokens:
        return 0.0
    return len(statement_tokens & task_tokens) / len(statement_tokens)


def analyze_message(
    connection: sqlite3.Connection,
    *,
    conversation_id: str,
    message: dict[str, Any],
    projects: list[ProjectRecord] | None = None,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    """Extracts and stores grounded facts for one message.

    Only user-authored content is analysed. Assistant messages are ignored so model
    assumptions can never become stored tasks, progress or completions.
    """
    stats = {"tasks": 0, "duplicates": 0, "proposals": 0}
    if (message.get("role") or "").casefold() != "user":
        return stats
    projects = projects if projects is not None else project_index(connection)
    reference = reference_time or _message_time(message.get("created_at"))
    facts = extract_facts(message.get("content") or "", reference_time=reference, projects=projects)

    for candidate in facts["projects"]:
        existing = candidate["existing"]
        if existing is not None:
            if candidate["name"].casefold() != existing.name.casefold():
                database.add_project_alias(
                    connection,
                    existing.id,
                    candidate["name"],
                    source="detected",
                )
            continue
        proposal = database.create_intelligence_proposal(
            connection,
            kind="project",
            title=candidate["name"],
            detail=candidate["detail"],
            candidate_project_name=candidate["name"],
            confidence=candidate["confidence"],
            conversation_id=conversation_id,
            message_id=message.get("id"),
            message_created_at=message.get("created_at"),
            excerpt=candidate["detail"],
            evidence=[_evidence(conversation_id, message, candidate["detail"])],
            fingerprint=_fingerprint("project", candidate["name"]),
        )
        if proposal is not None:
            stats["proposals"] += 1

    for task in facts["tasks"]:
        created = _record_task(connection, task, conversation_id=conversation_id, message=message)
        if created is None:
            stats["duplicates"] += 1
        else:
            stats["tasks"] += 1

    for blocker in facts["blockers"]:
        project = blocker["project"]
        proposal = database.create_intelligence_proposal(
            connection,
            kind="blocker",
            title=blocker["title"],
            detail=blocker["detail"],
            project_id=project.id if project is not None else None,
            confidence=blocker["confidence"],
            conversation_id=conversation_id,
            message_id=message.get("id"),
            message_created_at=message.get("created_at"),
            excerpt=blocker["detail"],
            evidence=[_evidence(conversation_id, message, blocker["detail"])],
            fingerprint=_fingerprint("blocker", project.id if project is not None else "", blocker["title"]),
        )
        if proposal is not None:
            stats["proposals"] += 1

    for completion in facts["completions"]:
        project = completion["project"]
        candidate_task = _completion_candidate(
            connection,
            completion["title"],
            project.id if project is not None else None,
        )
        if candidate_task is None:
            continue
        proposal = database.create_intelligence_proposal(
            connection,
            kind="task_completion",
            title=candidate_task["title"],
            detail=completion["detail"],
            project_id=candidate_task["project_id"],
            task_id=candidate_task["id"],
            confidence=completion["confidence"],
            conversation_id=conversation_id,
            message_id=message.get("id"),
            message_created_at=message.get("created_at"),
            excerpt=completion["detail"],
            evidence=[_evidence(conversation_id, message, completion["detail"])],
            fingerprint=_fingerprint("completion", candidate_task["id"], completion["detail"]),
        )
        if proposal is not None:
            stats["proposals"] += 1

    for progress in facts["progress"]:
        project = progress["project"]
        if project is None:
            continue
        proposal = database.create_intelligence_proposal(
            connection,
            kind="project_progress",
            title=f"{project.name}: {progress['percent']}%",
            detail=progress["detail"],
            project_id=project.id,
            progress_value=progress["percent"],
            confidence=progress["confidence"],
            conversation_id=conversation_id,
            message_id=message.get("id"),
            message_created_at=message.get("created_at"),
            excerpt=progress["detail"],
            evidence=[_evidence(conversation_id, message, progress["detail"])],
            fingerprint=_fingerprint("progress", project.id, str(progress["percent"]), progress["detail"]),
        )
        if proposal is not None:
            stats["proposals"] += 1

    for milestone in facts["milestones"]:
        project = milestone["project"]
        due = milestone["due"]
        proposal = database.create_intelligence_proposal(
            connection,
            kind="milestone",
            title=milestone["title"],
            detail=milestone["detail"],
            project_id=project.id,
            due_at=due.due_at if due else None,
            due_phrase=due.phrase if due else None,
            milestone_target_date=due.due_at[:10] if due else None,
            confidence=milestone["confidence"],
            conversation_id=conversation_id,
            message_id=message.get("id"),
            message_created_at=message.get("created_at"),
            excerpt=milestone["detail"],
            evidence=[_evidence(conversation_id, message, milestone["detail"])],
            fingerprint=_fingerprint("milestone", project.id, milestone["title"]),
        )
        if proposal is not None:
            stats["proposals"] += 1

    return stats


# ---------------------------------------------------------------------------
# Optional local model assistance
# ---------------------------------------------------------------------------


def _grounded_in_message(title: str, deadline_phrase: str | None, content: str) -> bool:
    title_tokens = _content_tokens(title)
    if not title_tokens:
        return False
    content_tokens = _content_tokens(content)
    if not content_tokens:
        return False
    if len(title_tokens & content_tokens) / len(title_tokens) < 0.8:
        return False
    if deadline_phrase and deadline_phrase.casefold() not in content.casefold():
        return False
    return True


async def model_task_hints(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Asks the local Ollama model for extra tasks, keeping only grounded suggestions.

    Any suggestion whose title is not built from words in the message, or whose deadline
    phrase is not copied verbatim from the message, is discarded. Model output can add
    proposals; it can never add a deadline, progress value or completion on its own.
    """
    if not messages:
        return {}
    payload = "\n".join(
        f"- id={message['id']} at={message['created_at']}\n  text: {message['content'][:1200]}"
        for message in messages
    )
    prompt = (
        "Extract only work the author says they must do. Reply with JSON only using this shape: "
        '{"tasks":[{"message_id":"...","title":"...","deadline_phrase":null,"priority":"low|medium|high|urgent"}]}. '
        "Copy deadline_phrase verbatim from the message text and use null when no deadline is written. "
        "Never invent a deadline, project, progress value or completion. Omit anything uncertain. "
        f"Messages:\n{payload}"
    )
    try:
        reply = await ollama.chat(
            [
                {
                    "role": "system",
                    "content": "You extract grounded tasks from a user's own messages. Output JSON only.",
                },
                {"role": "user", "content": prompt},
            ]
        )
    except Exception as exc:  # noqa: BLE001 - model assistance is optional and never fatal
        logger.info("Intelligence model pass skipped (%s).", type(exc).__name__)
        return {}

    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(reply[start : end + 1])
    except ValueError:
        return {}
    raw_tasks = parsed.get("tasks") if isinstance(parsed, dict) else None
    if not isinstance(raw_tasks, list):
        return {}
    by_message: dict[str, list[dict[str, Any]]] = {}
    for item in raw_tasks[:20]:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("message_id") or "")
        title = " ".join(str(item.get("title") or "").split())[:MAX_TASK_TITLE]
        deadline_phrase = item.get("deadline_phrase")
        if isinstance(deadline_phrase, str):
            deadline_phrase = " ".join(deadline_phrase.split()) or None
        else:
            deadline_phrase = None
        priority = str(item.get("priority") or "medium").casefold()
        if priority not in PRIORITY_RANK:
            priority = "medium"
        message = next((entry for entry in messages if entry["id"] == message_id), None)
        if message is None or not _valid_title(title):
            continue
        if not _grounded_in_message(title, deadline_phrase, message["content"]):
            continue
        by_message.setdefault(message_id, []).append(
            {"title": title, "deadline_phrase": deadline_phrase, "priority": priority}
        )
    return by_message


# ---------------------------------------------------------------------------
# Incremental scanning
# ---------------------------------------------------------------------------


async def scan_conversations(
    connection: sqlite3.Connection,
    *,
    conversation_id: str | None = None,
    max_messages: int | None = None,
    mode: str = "incremental",
    use_model: bool = False,
) -> dict[str, Any]:
    """Processes only messages past the stored checkpoint for each conversation."""
    if mode not in {"incremental", "reprocess"}:
        raise ValueError("Unsupported scan mode.")
    if mode == "reprocess" and not conversation_id:
        raise ValueError("Reprocessing requires one explicit conversation id.")

    if conversation_id:
        exists = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if exists is None:
            raise ValueError("Conversation not found.")
        if mode == "reprocess":
            database.reset_intelligence_checkpoint(connection, conversation_id)

    budget = max_messages or settings.intelligence_max_messages_per_scan
    budget = max(1, min(int(budget), 5000))
    run = {
        "id": str(uuid4()),
        "started_at": _now(),
        "mode": "reprocess" if mode == "reprocess" else "incremental",
        "status": "succeeded",
        "conversations_scanned": 0,
        "messages_scanned": 0,
        "messages_skipped": 0,
        "tasks_detected": 0,
        "duplicates_skipped": 0,
        "proposals_created": 0,
        "model_used": False,
        "error": None,
    }

    if conversation_id:
        targets = [conversation_id]
    else:
        targets = [
            row["conversation_id"]
            for row in database.list_conversations_with_unprocessed_messages(connection)
        ]
    projects = project_index(connection)

    try:
        for target in targets:
            if budget <= 0:
                break
            conversation_scanned = False
            while budget > 0:
                checkpoint = database.get_intelligence_checkpoint(connection, target)
                after_rowid = checkpoint["last_message_rowid"] if checkpoint else 0
                batch = database.list_messages_after(
                    connection, target, after_rowid, min(50, budget)
                )
                if not batch:
                    break
                conversation_scanned = True
                stats = {"tasks": 0, "duplicates": 0, "proposals": 0}
                hints: dict[str, list[dict[str, Any]]] = {}
                user_messages = [item for item in batch if item["role"] == "user"]
                if use_model and user_messages:
                    hints = await model_task_hints(user_messages)
                    run["model_used"] = run["model_used"] or bool(hints)
                for message in batch:
                    run["messages_scanned"] += 1
                    budget -= 1
                    if message["role"] != "user":
                        run["messages_skipped"] += 1
                        continue
                    message_stats = analyze_message(
                        connection,
                        conversation_id=target,
                        message=message,
                        projects=projects,
                        reference_time=_message_time(message["created_at"]),
                    )
                    for key in stats:
                        stats[key] += message_stats.get(key, 0)
                for message in user_messages:
                    for hint in hints.get(message["id"], []):
                        due = resolve_deadline(
                            hint["deadline_phrase"] or "",
                            _message_time(message["created_at"]),
                        )
                        title = _clean_title(hint["title"], due)
                        if not _valid_title(title):
                            continue
                        project, score, ambiguous = match_project(message["content"], projects)
                        task = ExtractedTask(
                            title=title,
                            detail=_excerpt(message["content"]),
                            due=due,
                            priority=hint["priority"],
                            priority_explicit=True,
                            project_id=project.id if project is not None and not ambiguous else None,
                            project_name=project.name if project else None,
                            project_score=score,
                            confidence=0.6,
                            note="Suggested by the local model from your message; confirm it.",
                            sentence=_excerpt(message["content"]),
                            fingerprint=_task_fingerprint(
                                project.id if project is not None and not ambiguous else None,
                                title,
                                due.due_at if due else None,
                            ),
                        )
                        existing = _similar_existing_task(
                            connection,
                            task.project_id,
                            task.title,
                            due.due_at if due else None,
                            task.fingerprint,
                        )
                        if existing is not None:
                            stats["duplicates"] += 1
                            continue
                        created = _record_task(
                            connection, task, conversation_id=target, message=message
                        )
                        if created is None:
                            stats["duplicates"] += 1
                        else:
                            stats["tasks"] += 1
                last = batch[-1]
                database.save_intelligence_checkpoint(
                    connection,
                    target,
                    last_message_rowid=last["rowid"],
                    last_message_id=last["id"],
                    last_message_created_at=last["created_at"],
                    processed_increment=len(batch),
                )
                run["tasks_detected"] += stats["tasks"]
                run["duplicates_skipped"] += stats["duplicates"]
                run["proposals_created"] += stats["proposals"]
            if conversation_scanned:
                run["conversations_scanned"] += 1
    except (sqlite3.Error, OSError) as exc:
        logger.exception("Intelligence scan failed.")
        run["status"] = "failed"
        run["error"] = f"{type(exc).__name__}: {exc}"[:300]
        database.record_intelligence_run(connection, run)
        raise

    database.record_intelligence_run(connection, run)
    return run


# ---------------------------------------------------------------------------
# Proposal review
# ---------------------------------------------------------------------------


def _workspace_for_proposal(proposal: dict[str, Any], workspaces: dict[str, Any]) -> str:
    hint = (proposal.get("workspace_hint") or "").casefold()
    if hint and hint in workspaces:
        return hint
    candidate_tokens = _content_tokens(proposal.get("candidate_project_name") or proposal["title"])
    for workspace_id in workspaces:
        workspace_tokens = _content_tokens(workspace_id.replace("-", " ").replace("_", " "))
        if candidate_tokens and workspace_tokens and candidate_tokens <= workspace_tokens:
            return workspace_id
    if len(workspaces) == 1:
        return next(iter(workspaces))
    if not workspaces:
        raise ValueError("No authorized project workspace is configured for new projects.")
    raise ValueError("Choose an authorized project workspace before accepting this project.")


def apply_proposal(
    connection: sqlite3.Connection,
    proposal: dict[str, Any],
    *,
    workspaces: dict[str, Any],
) -> dict[str, Any]:
    """Applies an accepted proposal using the existing project and task services."""
    kind = proposal["kind"]
    project_id = proposal["project_id"]

    if kind == "project":
        name = proposal.get("candidate_project_name") or proposal["title"]
        existing, score = match_candidate_name(name, project_index(connection))
        if existing is not None:
            database.mark_intelligence_proposal(
                connection,
                proposal["id"],
                "accepted",
                result_id=existing.id,
                review_note="Matched an existing project instead of creating a duplicate.",
            )
            return {"kind": kind, "project_id": existing.id, "created": False}
        workspace_id = _workspace_for_proposal(proposal, workspaces)
        created = database.create_project(
            connection,
            project_id=str(uuid4()),
            name=name,
            description=proposal.get("detail", ""),
            workspace_id=workspace_id,
            status="active",
            priority="medium",
            progress=0,
            start_date=None,
            target_date=None,
            tech_stack=[],
            github_url=None,
        )
        alias = " ".join(name.casefold().split())
        if alias != created["name"].casefold():
            database.add_project_alias(connection, created["id"], alias, source="detected")
        database.mark_intelligence_proposal(
            connection, proposal["id"], "accepted", result_id=created["id"]
        )
        return {"kind": kind, "project_id": created["id"], "created": True}

    if kind == "task_completion":
        task_id = proposal.get("task_id")
        if not task_id:
            raise ValueError("This completion proposal is not linked to a task.")
        completed = database.complete_task(connection, task_id)
        if completed is None:
            raise ValueError("The linked task is no longer open.")
        database.set_task_completion_evidence(
            connection,
            task_id,
            proposal.get("excerpt") or proposal.get("detail") or "Confirmed completion statement.",
        )
        database.mark_intelligence_proposal(
            connection, proposal["id"], "accepted", result_id=task_id
        )
        return {"kind": kind, "task_id": task_id, "project_id": completed["project_id"]}

    if kind == "project_progress":
        if not project_id or database.get_project(connection, project_id) is None:
            raise ValueError("The linked project no longer exists.")
        value = proposal.get("progress_value")
        if value is None or not 0 <= int(value) <= 100:
            raise ValueError("The proposed progress value is invalid.")
        updated = database.update_project(connection, project_id, {"progress": int(value)})
        if updated is None:
            raise ValueError("The linked project could not be updated.")
        database.mark_intelligence_proposal(
            connection, proposal["id"], "accepted", result_id=project_id
        )
        return {"kind": kind, "project_id": project_id, "progress": updated["progress"]}

    if kind == "blocker":
        if project_id and database.get_project(connection, project_id) is None:
            raise ValueError("The linked project no longer exists.")
        created = database.create_task(
            connection,
            proposal["title"],
            proposal.get("excerpt") or proposal.get("detail", ""),
            conversation_id=proposal.get("conversation_id"),
            project_id=project_id,
            origin="conversation",
            confirmation_status="confirmed",
            confidence=proposal.get("confidence"),
            source_message_id=proposal.get("message_id"),
            source_excerpt=proposal.get("excerpt"),
            detected_at=_now(),
        )
        blocked = database.update_task_status(connection, created["id"], "blocked")
        database.mark_intelligence_proposal(
            connection, proposal["id"], "accepted", result_id=created["id"]
        )
        return {"kind": kind, "task_id": created["id"], "task": blocked, "project_id": project_id}

    if kind == "milestone":
        if not project_id or database.get_project(connection, project_id) is None:
            raise ValueError("The linked project no longer exists.")
        milestone = database.save_project_milestone(
            connection,
            project_id,
            proposal["title"],
            proposal.get("detail", ""),
            proposal.get("milestone_target_date"),
        )
        database.mark_intelligence_proposal(
            connection,
            proposal["id"],
            "accepted",
            result_id=milestone["id"] if milestone else None,
        )
        return {"kind": kind, "project_id": project_id, "milestone": milestone}

    raise ValueError("This proposal type cannot be applied automatically.")


def review_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    approve: bool,
    workspaces: dict[str, Any],
) -> dict[str, Any]:
    proposal = database.get_intelligence_proposal(connection, proposal_id)
    if proposal is None:
        raise LookupError("Proposal not found.")
    if proposal["status"] != "pending":
        raise ValueError("This proposal was already reviewed.")
    if not approve:
        updated = database.mark_intelligence_proposal(
            connection, proposal_id, "rejected", review_note="Rejected by the owner."
        )
        return {"proposal": updated, "applied": None}
    applied = apply_proposal(connection, proposal, workspaces=workspaces)
    updated = database.get_intelligence_proposal(connection, proposal_id)
    return {"proposal": updated, "applied": applied}


# ---------------------------------------------------------------------------
# Summaries and status
# ---------------------------------------------------------------------------


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "lifecycle": task["lifecycle"],
        "priority": task["priority"],
        "due_at": task["due_at"],
        "project_id": task["project_id"],
        "confirmation_status": task.get("confirmation_status"),
        "confidence": task.get("confidence"),
        "conversation_id": task.get("conversation_id"),
        "source_message_id": task.get("source_message_id"),
    }


def _sort_open_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        tasks,
        key=lambda task: (
            PRIORITY_RANK.get(task["priority"], 2),
            task["due_at"] is None,
            task["due_at"] or "",
        ),
    )


def build_summary(
    connection: sqlite3.Connection,
    *,
    project_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deterministic, record-backed intelligence summary. No model text is invented."""
    reference = time_context.current_time(now)
    horizon = reference + timedelta(days=UPCOMING_WINDOW_DAYS)
    projects = [project for project in database.list_projects(connection) if project["status"] != "archived"]
    if project_id:
        projects = [project for project in projects if project["id"] == project_id]
        if not projects:
            raise LookupError("Project not found.")
    tasks = database.list_tasks(connection)
    pending_proposals = database.list_intelligence_proposals(connection, status="pending", limit=200)

    project_summaries: list[dict[str, Any]] = []
    all_priorities: list[dict[str, Any]] = []
    all_upcoming: list[dict[str, Any]] = []
    all_progress: list[dict[str, Any]] = []
    all_outstanding: list[dict[str, Any]] = []
    all_risks: list[dict[str, Any]] = []

    for project in projects:
        scoped_tasks = [task for task in tasks if task["project_id"] == project["id"]]
        open_tasks = [task for task in scoped_tasks if task["status"] != "done"]
        done_tasks = [task for task in scoped_tasks if task["status"] == "done"]
        overdue = [task for task in open_tasks if task["lifecycle"] == "overdue"]
        blocked = [task for task in open_tasks if task["status"] == "blocked"]
        unconfirmed = [task for task in open_tasks if task["lifecycle"] in {"detected", "pending_confirmation"}]
        upcoming = []
        for task in open_tasks:
            due_at = database.parse_timestamp(task["due_at"])
            if due_at is not None and reference <= due_at <= horizon:
                upcoming.append(task)
        upcoming.sort(key=lambda task: task["due_at"] or "")

        milestones = database.list_project_milestones(connection, project["id"])
        next_milestone = next(
            (item for item in milestones if item["status"] != "completed"), None
        )
        completed_milestones = [item for item in milestones if item["status"] == "completed"]
        notes = database.list_project_notes(connection, project["id"])[:3]

        due_risks = list(overdue) + list(blocked)
        risks: list[dict[str, Any]] = []
        for task in due_risks:
            risks.append(
                {
                    "severity": "high",
                    "kind": "overdue_task" if task["lifecycle"] == "overdue" else "blocked_task",
                    "summary": (
                        f"Task '{task['title']}' is overdue."
                        if task["lifecycle"] == "overdue"
                        else f"Task '{task['title']}' is blocked."
                    ),
                    "task_id": task["id"],
                    "project_id": project["id"],
                    "evidence": {"conversation_id": task.get("conversation_id"), "message_id": task.get("source_message_id")},
                }
            )
        target = database.parse_timestamp(project["target_date"])
        if target is not None and target < reference and project["status"] in {"active", "planned"}:
            risks.append(
                {
                    "severity": "high",
                    "kind": "target_date_passed",
                    "summary": f"Target date {project['target_date']} passed while the project is still {project['status']}.",
                    "project_id": project["id"],
                }
            )
        last_activity = database.parse_timestamp(project["last_activity_at"])
        if (
            last_activity is not None
            and project["status"] == "active"
            and (reference - last_activity).days >= STALE_PROJECT_DAYS
        ):
            risks.append(
                {
                    "severity": "medium",
                    "kind": "stale_project",
                    "summary": f"No recorded activity for {(reference - last_activity).days} days.",
                    "project_id": project["id"],
                }
            )
        if unconfirmed:
            risks.append(
                {
                    "severity": "medium",
                    "kind": "unconfirmed_work",
                    "summary": f"{len(unconfirmed)} detected task(s) still need confirmation.",
                    "project_id": project["id"],
                }
            )

        recent_progress: list[dict[str, Any]] = [
            {
                "kind": "task_completed",
                "summary": f"Completed task '{task['title']}'.",
                "at": task["completed_at"],
                "task_id": task["id"],
                "evidence": {"conversation_id": task.get("conversation_id"), "message_id": task.get("source_message_id")},
            }
            for task in sorted(done_tasks, key=lambda item: item["completed_at"] or "", reverse=True)[:5]
        ]
        recent_progress.extend(
            {
                "kind": "milestone_completed",
                "summary": f"Reached milestone '{item['title']}'.",
                "at": item["completed_at"],
                "milestone_id": item["id"],
            }
            for item in completed_milestones[:5]
        )
        recent_progress.extend(
            {
                "kind": "project_note",
                "summary": f"[{item['category']}] {item['title']}",
                "at": item["updated_at"],
                "note_id": item["id"],
            }
            for item in notes
        )
        recent_progress.sort(key=lambda item: item.get("at") or "", reverse=True)

        summary_line = (
            f"{project['name']} is {project['status']} at {project['progress']}% progress. "
            f"{len(open_tasks)} open task(s), {len(done_tasks)} completed, "
            f"{len(blocked)} blocked, {len(overdue)} overdue."
        )
        if project["description"].strip():
            summary_line += f" {project['description'].strip()[:400]}"
        if next_milestone is not None:
            summary_line += (
                f" Next milestone: {next_milestone['title']}"
                + (f" by {next_milestone['target_date']}." if next_milestone["target_date"] else ".")
            )

        priorities = [_task_view(task) for task in _sort_open_tasks(open_tasks)[:10]]
        project_summaries.append(
            {
                "project_id": project["id"],
                "name": project["name"],
                "status": project["status"],
                "priority": project["priority"],
                "progress": project["progress"],
                "target_date": project["target_date"],
                "summary": summary_line,
                "current_priorities": priorities,
                "upcoming_deadlines": [_task_view(task) for task in upcoming[:10]],
                "recent_progress": recent_progress[:8],
                "outstanding_work": [_task_view(task) for task in _sort_open_tasks(open_tasks)],
                "confirmed_task_count": len(scoped_tasks) - len(unconfirmed),
                "unconfirmed_task_count": len(unconfirmed),
                "risks": risks,
            }
        )
        all_priorities.extend(priorities)
        all_upcoming.extend(_task_view(task) for task in upcoming[:10])
        all_progress.extend(recent_progress[:8])
        all_outstanding.extend(_task_view(task) for task in open_tasks)
        all_risks.extend(risks)

    unassigned = [task for task in tasks if task["project_id"] is None]
    for task in unassigned:
        all_priorities.append(_task_view(task))
        all_outstanding.append(_task_view(task))
        due_at = database.parse_timestamp(task["due_at"])
        if due_at is not None and reference <= due_at <= horizon:
            all_upcoming.append(_task_view(task))
    if unassigned:
        all_risks.append(
            {
                "severity": "medium",
                "kind": "unassigned_work",
                "summary": f"{len(unassigned)} task(s) are not linked to a project.",
            }
        )

    seen: set[str] = set()
    unique_priorities = []
    for task in _sort_open_tasks(all_priorities):
        if task["id"] in seen:
            continue
        seen.add(task["id"])
        unique_priorities.append(task)
    unique_upcoming = []
    seen.clear()
    for task in sorted(all_upcoming, key=lambda item: item["due_at"] or ""):
        if task["id"] in seen:
            continue
        seen.add(task["id"])
        unique_upcoming.append(task)

    pending_by_kind: dict[str, int] = {}
    for proposal in pending_proposals:
        pending_by_kind[proposal["kind"]] = pending_by_kind.get(proposal["kind"], 0) + 1

    return {
        "generated_at": reference.isoformat(),
        "scope": {"project_id": project_id},
        "projects": project_summaries,
        "current_priorities": unique_priorities[:15],
        "upcoming_deadlines": unique_upcoming[:15],
        "recent_progress": sorted(all_progress, key=lambda item: item.get("at") or "", reverse=True)[:15],
        "outstanding_work": all_outstanding[:30],
        "risks": all_risks[:20],
        "pending_confirmations": {
            "tasks": len([task for task in tasks if task["lifecycle"] in {"detected", "pending_confirmation"}]),
            "proposals": len(pending_proposals),
            "by_kind": pending_by_kind,
        },
    }


def status_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """Cheap status for the dashboard; it never scans conversations."""
    checkpoints = connection.execute(
        "SELECT COUNT(*) AS conversations, COALESCE(SUM(messages_processed), 0) AS processed "
        "FROM intelligence_checkpoints"
    ).fetchone()
    pending = database.list_intelligence_proposals(connection, status="pending", limit=500)
    by_kind: dict[str, int] = {}
    for proposal in pending:
        by_kind[proposal["kind"]] = by_kind.get(proposal["kind"], 0) + 1
    tasks = database.list_tasks(connection)
    runs = database.list_intelligence_runs(connection, 1)
    return {
        "checkpoints": {
            "conversations": int(checkpoints["conversations"]),
            "messages_processed": int(checkpoints["processed"]),
            "unprocessed_messages": database.count_unprocessed_messages(connection),
        },
        "tasks": {
            "detected": len([task for task in tasks if task["lifecycle"] == "detected"]),
            "pending_confirmation": len(
                [task for task in tasks if task["lifecycle"] == "pending_confirmation"]
            ),
            "upcoming": len([task for task in tasks if task["lifecycle"] == "upcoming"]),
            "overdue": len([task for task in tasks if task["lifecycle"] == "overdue"]),
            "blocked": len([task for task in tasks if task["lifecycle"] == "blocked"]),
            "in_progress": len([task for task in tasks if task["lifecycle"] == "in_progress"]),
            "completed": len([task for task in tasks if task["lifecycle"] == "completed"]),
            "archived": int(
                connection.execute(
                    """SELECT COUNT(*) AS count FROM application_tasks
                       WHERE archived_at IS NOT NULL AND deleted_at IS NULL"""
                ).fetchone()["count"]
            ),
        },
        "proposals": {"pending": len(pending), "by_kind": by_kind},
        "last_run": runs[0] if runs else None,
        "schedule": {
            "auto_scan_interval_minutes": settings.intelligence_scan_interval_minutes,
            "max_messages_per_scan": settings.intelligence_max_messages_per_scan,
        },
    }


async def intelligence_worker(stop: asyncio.Event) -> None:
    """Periodic incremental scan so new conversations are processed without a request."""
    interval_minutes = settings.intelligence_scan_interval_minutes
    if interval_minutes <= 0:
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_minutes * 60)
        except TimeoutError:
            pass
        else:
            return
        try:
            with database.session() as connection:
                await scan_conversations(connection)
        except (sqlite3.Error, OSError):
            logger.exception("Scheduled intelligence scan could not run.")
