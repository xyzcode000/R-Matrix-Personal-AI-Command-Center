"""Query understanding for document questions.

This layer runs before retrieval. It normalizes informal spellings of document
fields, classifies which document type and field(s) a question concerns, resolves
elliptical follow-ups from recent conversation turns, and flags repeated questions
that should widen their retrieval instead of repeating an identical answer.

It is intentionally deterministic and local: no model call, no network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

# Common informal spellings map onto the canonical term used by retrieval and
# field extraction. These are query concepts only; they never create facts.
TOKEN_ALIASES = {
    "aadhar": "aadhaar",
    "adhar": "aadhaar",
    "aashaar": "aadhaar",
    "aashar": "aadhaar",
    "adharcard": "aadhaar card",
    "aadharcard": "aadhaar card",
    "uidai": "aadhaar",
    "uid": "aadhaar",
    "pancard": "pan card",
    "dob": "date of birth",
    "birthday": "date of birth",
    "dad": "father",
    "papa": "father",
    "mom": "mother",
    "mum": "mother",
    "mata": "mother",
    "maths": "mathematics",
    "math": "mathematics",
    "licence": "license",
    "dl": "driving license",
    "12th": "class 12",
    "10th": "class 10",
}

FIELD_PATTERNS: tuple[tuple[str, str], ...] = (
    ("aadhaar_number", r"\baadhaar\b"),
    ("pan_number", r"\bpan\b"),
    ("father_name", r"\bfather\b"),
    ("mother_name", r"\bmother\b"),
    ("date_of_birth", r"\bdate of birth\b"),
    ("board", r"\bboard\b"),
    ("total_marks", r"\btotal\b|\bpercentage\b"),
)

DOCUMENT_FIELD_LISTS: dict[str, tuple[str, ...]] = {
    "aadhaar": (
        "name", "date_of_birth", "gender", "aadhaar_number",
        "father_name", "mother_name", "address",
    ),
    "pan": ("name", "father_name", "date_of_birth", "pan_number"),
    "marksheet": (
        "student_name", "roll_number", "subjects", "marks_obtained",
        "maximum_marks", "grade", "result", "board", "year",
    ),
    "migration": ("student_name", "roll_number", "from_institution", "to_institution", "issue_date"),
    "driving_licence": ("name", "date_of_birth", "licence_number", "vehicle_classes", "validity"),
}

DOCUMENT_TYPE_LABELS = {
    "aadhaar": "Aadhaar card",
    "pan": "PAN card",
    "marksheet": "marksheet",
    "migration": "migration certificate",
    "driving_licence": "driving licence",
}

SUBJECT_NAMES = (
    "mathematics", "english", "hindi", "science", "physics", "chemistry",
    "biology", "social science", "history", "geography", "economics",
    "accountancy", "business studies", "computer science", "computer",
    "information technology", "physical education", "sanskrit", "marathi",
    "urdu", "commerce", "statistics",
)

_ELLIPTICAL = re.compile(
    r"^(?:(?:give|show|tell|get|fetch|read|find)\s+(?:me\s+)?|what\s+about\s+|"
    r"and\s+|same\s+|it\b|that\b|this\b|again\b|try\b|any\b|anything\b|"
    r"from\s+any\b|use\s+any\b|number\b|no\b)",
    re.IGNORECASE,
)

_WIDEN = re.compile(
    r"\b(?:any|anything|all|again|try|retry|other|different|wider|elsewhere|everywhere|"
    r"from any|no matter)\b",
    re.IGNORECASE,
)

_OCR_CONFUSIONS = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "l": "i",
        "5": "s",
        "8": "b",
        "q": "g",
        "u": "v",
        "y": "w",
        "z": "s",
        "j": "i",
    }
)


@dataclass(frozen=True)
class QueryIntent:
    """Deterministic interpretation of one chat message."""

    original: str
    resolved: str
    fields: frozenset[str]
    document_types: frozenset[str]
    widen: bool = False
    inherited: bool = False

    @property
    def is_document_question(self) -> bool:
        return bool(self.fields or self.document_types)


def canonicalize_query(text: str) -> str:
    """Folds informal spellings onto canonical field and document terms."""
    value = (text or "").replace("\u2019", "'").casefold()
    value = re.sub(r"\b(father|mother|dad|mom|mum|papa|mata)'s\b", r"\1", value)
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", value):
        replacement = TOKEN_ALIASES.get(token, token)
        tokens.extend(replacement.split())
    return " ".join(tokens)


def _has_subject_marks(folded: str) -> bool:
    return bool(
        re.search(
            r"\b(?:marks?|score|scores|percentage|grade|result)\b", folded
        )
    )


def subject_for_query(text: str) -> str | None:
    folded = canonicalize_query(text)
    for name in SUBJECT_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", folded):
            return name
    return None


def fields_for_query(text: str) -> frozenset[str]:
    folded = canonicalize_query(text)
    fields: set[str] = set()
    for name, pattern in FIELD_PATTERNS:
        if re.search(pattern, folded):
            fields.add(name)
    if _has_subject_marks(folded):
        fields.add("subject_marks")
    if re.search(r"\bclass (?:10|12)\b", folded):
        fields.add("board")
    # A question about the contents of a named document asks for all its fields.
    return frozenset(fields)


def document_types_for_query(text: str) -> frozenset[str]:
    folded = canonicalize_query(text)
    types: set[str] = set()
    if re.search(r"\baadhaar\b", folded):
        types.add("aadhaar")
    if re.search(r"\bpan\b", folded):
        types.add("pan")
    if re.search(
        r"\b(?:board|marksheet|mark sheet|marks statement|class 1[02]|exam|examination|"
        r"result|subject|mathematics|percentage|total marks|marks)\b",
        folded,
    ):
        types.add("marksheet")
    if re.search(r"\b(?:migration|transfer certificate)\b", folded):
        types.add("migration")
    if re.search(r"\bdriving license\b|\blicense number\b|\bvehicle\b", folded):
        types.add("driving_licence")
    if re.search(r"\bfather\b|\bmother\b|\bparent\b", folded):
        types |= {"pan", "aadhaar", "marksheet", "migration"}
    return frozenset(types)


def inspect_query(message: str) -> QueryIntent:
    original = message or ""
    folded = canonicalize_query(original)
    resolved_subject = subject_for_query(original)
    resolved = f"{folded} {resolved_subject}".strip() if resolved_subject else folded
    return QueryIntent(
        original=original,
        resolved=resolved,
        fields=fields_for_query(original),
        document_types=document_types_for_query(original),
        widen=bool(_WIDEN.search(folded)),
    )


def _looks_elliptical(message: str) -> bool:
    folded = canonicalize_query(message)
    if not folded:
        return False
    if _ELLIPTICAL.search(folded):
        return True
    # Very short fragments with no field or document word are follow-ups too.
    return len(folded.split()) <= 4


def resolve_follow_up(message: str, history: Iterable[dict[str, Any]] | None) -> QueryIntent:
    """Carries the subject of an earlier turn into an elliptical follow-up."""
    current = inspect_query(message)
    if current.is_document_question or not _looks_elliptical(message):
        return current
    recent = list(history or [])[-12:]
    for index in range(len(recent) - 1, -1, -1):
        item = recent[index]
        if item.get("role") != "user":
            continue
        prior = inspect_query(str(item.get("content") or ""))
        if not prior.is_document_question:
            continue
        resolved = " ".join(
            part for part in (current.resolved, prior.resolved) if part
        )
        return QueryIntent(
            original=message,
            resolved=resolved,
            fields=prior.fields,
            document_types=prior.document_types,
            widen=current.widen or prior.widen,
            inherited=True,
        )
    return current


def is_repeat_query(
    intent: QueryIntent,
    history: Iterable[dict[str, Any]] | None,
) -> bool:
    """True when the same subject was already asked and not confidently answered."""
    if not intent.is_document_question:
        return False
    recent = list(history or [])[-12:]
    for index, item in enumerate(recent):
        if item.get("role") != "user":
            continue
        prior = inspect_query(str(item.get("content") or ""))
        if not prior.is_document_question:
            continue
        field_overlap = bool(set(intent.fields) & set(prior.fields))
        same_type = bool(
            intent.document_types
            and set(intent.document_types) & set(prior.document_types)
        )
        if not (field_overlap or same_type):
            continue
        state = None
        for later in recent[index + 1 : index + 3]:
            if later.get("role") == "assistant":
                state = (later.get("execution") or {}).get("answer_state")
                break
        if state in {"confirmed", "answered", "user_correction"}:
            continue
        return True
    return False


def search_query(intent: QueryIntent) -> str:
    """Builds a bounded retrieval query containing canonical intent terms."""
    terms: list[str] = []
    for field in sorted(intent.fields):
        terms.extend(field.replace("_", " ").split())
    for document_type in sorted(intent.document_types):
        label = DOCUMENT_TYPE_LABELS.get(document_type, document_type)
        terms.extend(label.casefold().replace("_", " ").split())
    terms.extend(canonicalize_query(intent.original).split())
    deduped = list(dict.fromkeys(term for term in terms if term))
    return " ".join(deduped)[:300] or (intent.original or "")[:300]


def prompt_field_instruction(
    document_types: Iterable[str],
    fields: Iterable[str],
) -> str:
    """Describes the expected fields for the prompt, when a type is established."""
    types = [item for item in document_types if item in DOCUMENT_FIELD_LISTS]
    lines: list[str] = []
    for document_type in sorted(types):
        label = DOCUMENT_TYPE_LABELS.get(document_type, document_type)
        expected = ", ".join(DOCUMENT_FIELD_LISTS[document_type])
        lines.append(
            f"The user is asking about their {label}. Expected fields: {expected}. "
            "For each expected field, state whether it was readable, report the value "
            "when readable, and note when it is unclear or not present. Report every "
            "field, not only the first one."
        )
    selected = sorted(
        field for field in fields if field not in {"subject_marks", "total_marks", "marks"}
    )
    if selected:
        lines.append(
            "The current question specifically concerns: "
            + ", ".join(field.replace("_", " ") for field in selected)
            + "."
        )
    return "\n".join(lines)


def _levenshtein(first: str, second: str) -> int:
    if first == second:
        return 0
    if not first:
        return len(second)
    if not second:
        return len(first)
    previous = list(range(len(second) + 1))
    for i, left in enumerate(first, start=1):
        current = [i]
        for j, right in enumerate(second, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if left == right else 1),
                )
            )
        previous = current
    return previous[-1]


def _ocr_fold(value: str) -> str:
    folded = re.sub(r"[^a-z]", "", (value or "").casefold())
    return folded.replace("rn", "m").translate(_OCR_CONFUSIONS)


def _name_tokens(value: str) -> list[str]:
    tokens = [token for token in re.findall(r"[A-Za-z\u0900-\u097F]+", value or "")]
    while tokens and len(tokens[-1]) == 1:
        tokens.pop()
    return tokens


def names_are_ocr_variants(first: str, second: str) -> bool:
    """Whether two name readings plausibly differ only by OCR characters.

    Deliberately conservative: the readings must have the same word count, similar
    length, and an edit distance of at most two after folding common OCR
    substitutions (0/O, 1/I/l, 5/S, 8/B, Q/G, U/V, Y/W, trailing initials).
    """
    left = _name_tokens(first)
    right = _name_tokens(second)
    if not left or not right or len(left) != len(right):
        return False
    if [token.casefold() for token in left] == [token.casefold() for token in right]:
        return True
    left_folded = " ".join(_ocr_fold(token) for token in left)
    right_folded = " ".join(_ocr_fold(token) for token in right)
    if left_folded == right_folded:
        return True
    if not left_folded or not right_folded:
        return False
    longest = max(len(left_folded), len(right_folded))
    shortest = min(len(left_folded), len(right_folded))
    if shortest / longest < 0.6:
        return False
    allowed = 2 if longest >= 8 else 1
    return _levenshtein(left_folded, right_folded) <= allowed
