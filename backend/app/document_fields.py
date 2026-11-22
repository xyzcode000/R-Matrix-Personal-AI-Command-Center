"""Structured, evidence-first extraction for document questions.

This module works on the text of complete stored pages. It never invents a
value: every answer is one of confirmed, ambiguous, unreadable, not_found or
incomplete, and every citation points at the page the evidence came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import query_understanding

PAN_PATTERN = re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])")
AADHAAR_PATTERN = re.compile(r"(?<!\d)(\d{4}[\s-]?\d{4}[\s-]?\d{4})(?!\d)")
DATE_PATTERN = re.compile(r"([0-3]?\d[\s./-][01]?\d[\s./-](?:19|20)?\d{2})")

PAN_LABEL = re.compile(r"\bpan\b|permanent\s+account\s+number", re.IGNORECASE)
AADHAAR_LABEL = re.compile(r"aadha?ar|adhar|aashar|uidai|\buid\b|unique\s+identification", re.IGNORECASE)
DOB_LABEL = re.compile(r"date\s*of\s*birth|d\.?\s*o\.?\s*b\.?|\bdob\b|birth\s*date|जन्म", re.IGNORECASE)
BOARD_LABEL = re.compile(
    r"(?:board|university|council|education\s+board|school\s+board|institution|issuing\s+authority)"
    r"\s*(?:of\s+)?(?:secondary\s+education\s*)?[:\-–]?\s*([^\n]{3,90})",
    re.IGNORECASE,
)
BOARD_NAME_HINT = re.compile(
    r"(?:board\s+of\s+(?:secondary|school)\s+education|state\s+board|central\s+board|"
    r"cbse|cisce|icse|university|education\s+council)",
    re.IGNORECASE,
)
FATHER_LABEL = re.compile(r"(?:father'?s?|पिता)(?:\s+name)?\s*[:\-–]?\s*([^\n]{0,70})", re.IGNORECASE)
MOTHER_LABEL = re.compile(r"(?:mother'?s?|माता|माँ)(?:\s+name)?\s*[:\-–]?\s*([^\n]{0,70})", re.IGNORECASE)
NAME_LABEL = re.compile(r"(?:^|\n)\s*(?:name|नाम)\s*[:\-–]?\s*([^\n]{2,70})", re.IGNORECASE)
TOTAL_LABEL = re.compile(r"(?:grand\s+total|total\s+marks|total)\s*[:\-–]?\s*([0-9O]{1,4})\s*(?:/|out\s+of)\s*([0-9O]{2,4})", re.IGNORECASE)
PERCENT_LABEL = re.compile(r"(?:percentage|percent)\s*[:\-–]?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*%?", re.IGNORECASE)

SUBJECT_NAMES = (
    "mathematics", "maths", "math", "english", "hindi", "science", "physics",
    "chemistry", "biology", "social science", "social", "history", "geography",
    "economics", "accountancy", "business studies", "computer science",
    "computer", "information technology", "physical education", "sanskrit",
    "marathi", "urdu", "commerce", "statistics",
)
SUBJECT_QUERY = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in SUBJECT_NAMES) + r")\b",
    re.IGNORECASE,
)

# Standard Verhoeff tables, used only as an additional structural signal.
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


@dataclass
class Page:
    document_id: str
    document_name: str
    page_number: int
    text: str
    confidence: float | None = None
    status: str = "ok"


@dataclass
class Candidate:
    label: str
    value: str
    page: int
    document_id: str
    document_name: str
    confidence: float | None
    basis: str = ""
    alternatives: list[dict[str, Any]] = field(default_factory=list)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def verhoeff_valid(value: str) -> bool:
    number = digits(value)
    if len(number) != 12:
        return False
    checksum = 0
    for index, char in enumerate(reversed(number)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[index % 8][int(char)]]
    return checksum == 0


def _line_at(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return text[start:] if end < 0 else text[start:end]


def _near_label(text: str, pattern: re.Pattern[str], window: int = 90) -> bool:
    for match in pattern.finditer(text):
        segment = text[match.end() : match.end() + window]
        if segment.strip():
            return True
    return False


def _clean_value(value: str) -> str:
    value = value.replace("|", " ").replace("¦", " ").replace("_", " ")
    value = normalize(re.sub(r"^[\s:.\-–]+", "", value))
    value = re.split(r"\s{2,}", value)[0]
    return value.strip(" .,:;-/")


def _label_context(text: str, label: re.Pattern[str], *, next_line: bool = True) -> list[tuple[str, float]]:
    """Returns values on the label's line or immediately after it."""
    found: list[tuple[str, float]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = label.search(line)
        if not match:
            continue
        captured = match.group(1) if match.groups() else line[match.end() :]
        inline = _clean_value(captured)
        if inline and not re.fullmatch(r"[\s:.\-–]*", inline):
            found.append((inline, 1.0))
            continue
        if next_line and index + 1 < len(lines):
            following = _clean_value(lines[index + 1])
            if following:
                found.append((following, 0.85))
    return found


def _plausible_name(value: str) -> bool:
    folded = value.casefold()
    if len(value) < 2 or len(value) > 60:
        return False
    if "|" in value or "¦" in value:
        return False
    if re.search(
        r"\b(?:name|father|mother|address|date|birth|signature|card|number|aadhaar|pan|"
        r"guardian|beset|school|college|examination|marks|subject)\b",
        folded,
    ):
        return False
    if not re.search(r"[A-Za-z\u0900-\u097F]", value):
        return False
    if sum(char.isdigit() for char in value) > 2:
        return False
    return True


def pan_candidates(text: str) -> list[tuple[str, float]]:
    upper = (text or "").upper()
    found: dict[str, float] = {}
    for match in PAN_PATTERN.finditer(upper):
        found[match.group(0)] = 1.0
    # OCR sometimes separates the characters of a labelled PAN. Only accept
    # this relaxed form when a PAN label is present on the same page.
    if not found and PAN_LABEL.search(text or ""):
        spaced = re.compile(
            r"(?<![A-Z0-9])((?:[A-Z][\s.\-]?){5}(?:[0-9O][\s.\-]?){4}[A-Z])(?![A-Z])"
        )
        for match in spaced.finditer(upper):
            compact = re.sub(r"[^A-Z0-9]", "", match.group(1))
            compact = compact.replace("O", "0") if re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", compact.replace("O", "0")) else compact
            if PAN_PATTERN.fullmatch(compact):
                found[compact] = 0.6
    return list(found.items())


def aadhaar_candidates(text: str) -> list[tuple[str, float]]:
    found: dict[str, float] = {}
    for match in AADHAAR_PATTERN.finditer(text or ""):
        raw = match.group(1)
        number = digits(raw)
        if len(number) != 12 or number[0] in {"0", "1"}:
            continue
        line = _line_at(text, match.start())
        line_start = text.rfind("\n", 0, match.start()) + 1
        prefix = line[: max(0, match.start() - line_start)]
        labelled = bool(AADHAAR_LABEL.search(line)) or bool(
            re.search(r"(?:number|no\.?|uid)\s*[:\-–]?\s*$", prefix, re.IGNORECASE)
        )
        found[raw.strip()] = (0.95 if labelled else 0.55) + (0.05 if verhoeff_valid(number) else 0.0)
    return list(found.items())


def extract_names(pages: Iterable[Page], field_name: str) -> list[Candidate]:
    results: list[Candidate] = []
    for page in pages:
        values: list[tuple[str, float]] = []
        if "father" in field_name:
            values = _label_context(page.text, FATHER_LABEL)
        elif "mother" in field_name:
            values = _label_context(page.text, MOTHER_LABEL)
        else:
            for line in page.text.splitlines():
                match = re.match(r"\s*(?:full\s+)?name\s*[:\-–]?\s*(.+)$", line, re.IGNORECASE)
                if match:
                    values.append((match.group(1), 1.0))
        for value, position_weight in values:
            cleaned = _clean_value(value)
            if not _plausible_name(cleaned):
                continue
            confidence = page.confidence
            if confidence is not None:
                confidence = round(confidence * position_weight, 2)
            results.append(
                Candidate(
                    label=field_name,
                    value=cleaned,
                    page=page.page_number,
                    document_id=page.document_id,
                    document_name=page.document_name,
                    confidence=confidence,
                    basis="labelled value" if position_weight >= 1 else "value on the line after its label",
                )
            )
    return results


def extract_dob(pages: Iterable[Page]) -> list[Candidate]:
    results: list[Candidate] = []
    for page in pages:
        for match in DOB_LABEL.finditer(page.text):
            tail = page.text[match.end() : match.end() + 60]
            date = DATE_PATTERN.search(tail)
            if not date:
                continue
            results.append(
                Candidate(
                    label="date of birth",
                    value=date.group(1).strip(),
                    page=page.page_number,
                    document_id=page.document_id,
                    document_name=page.document_name,
                    confidence=page.confidence,
                    basis="date immediately after a birth-date label",
                )
            )
    return results


def extract_pan(pages: Iterable[Page]) -> list[Candidate]:
    results: list[Candidate] = []
    for page in pages:
        for value, quality in pan_candidates(page.text):
            results.append(
                Candidate(
                    label="PAN number",
                    value=value,
                    page=page.page_number,
                    document_id=page.document_id,
                    document_name=page.document_name,
                    confidence=round((page.confidence or 0) * quality, 2) if page.confidence is not None else None,
                    basis="label and PAN-shaped value on the same page" if quality >= 1 else "relaxed OCR spacing near a PAN label",
                )
            )
    return results


def extract_aadhaar(pages: Iterable[Page]) -> list[Candidate]:
    results: list[Candidate] = []
    for page in pages:
        for value, quality in aadhaar_candidates(page.text):
            confidence = page.confidence
            if confidence is not None:
                confidence = round(confidence * quality, 2)
            results.append(
                Candidate(
                    label="Aadhaar number",
                    value=value,
                    page=page.page_number,
                    document_id=page.document_id,
                    document_name=page.document_name,
                    confidence=confidence,
                    basis="twelve-digit group" + (" with a valid structural checksum" if verhoeff_valid(value) else ""),
                )
            )
    return results


def extract_board(pages: Iterable[Page]) -> list[Candidate]:
    results: list[Candidate] = []
    for page in pages:
        lines = page.text.splitlines()
        for line in lines:
            stripped = _clean_value(line)
            if not stripped or len(stripped) < 4:
                continue
            folded = stripped.casefold()
            if any(name in folded for name in SUBJECT_NAMES):
                continue
            labelled = re.search(r"\b(?:board|university|council)\b\s*[:\-–]\s*(.+)$", stripped, re.IGNORECASE)
            if labelled:
                value = _clean_value(labelled.group(1))
                if len(value) >= 2:
                    results.append(
                        Candidate(
                            label="education board",
                            value=value,
                            page=page.page_number,
                            document_id=page.document_id,
                            document_name=page.document_name,
                            confidence=page.confidence,
                            basis="explicit board label with a value",
                        )
                    )
                continue
            if BOARD_NAME_HINT.search(line):
                value = stripped
                if any(
                    token in folded
                    for token in (
                        "subject", "marks", "roll", "candidate", "father", "mother",
                        "taking examination", "established by law", "any university",
                        "prospectus", "eligible", "rules",
                    )
                ):
                    continue
                if len(value) > 80:
                    continue
                results.append(
                    Candidate(
                        label="education board",
                        value=value,
                        page=page.page_number,
                        document_id=page.document_id,
                        document_name=page.document_name,
                        confidence=page.confidence,
                        basis="named board or council printed on the page",
                    )
                )
    return results


def _numbers_in(line: str) -> list[int]:
    values: list[int] = []
    for token in re.findall(r"(?<![A-Za-z0-9])([0-9O]{1,4})(?![A-Za-z0-9])", line):
        cleaned = token.replace("O", "0")
        if cleaned.isdigit():
            values.append(int(cleaned))
    return values


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}


def number_words_value(text: str) -> int | None:
    """Reads a printed English number such as 'eighty four' (0-200)."""
    tokens = re.findall(
        r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", (text or "").casefold()
    )
    if not tokens:
        return None
    total = 0
    for token in tokens[:4]:
        value = _NUMBER_WORDS[token]
        if value == 100 and total:
            total *= 100
        else:
            total += value
    return total if 0 < total <= 200 else None


def extract_subject_rows(pages: Iterable[Page], subject: str) -> list[Candidate]:
    """Extracts obtained/maximum marks for one subject from table-like lines."""
    subject_folded = subject.casefold()
    results: list[Candidate] = []
    for page in pages:
        for line in page.text.splitlines():
            if subject_folded not in line.casefold():
                continue
            # Reject a passing mention such as "Mathematics" in a header only.
            numbers = _numbers_in(line)
            if not numbers:
                continue
            obtained: int | None = None
            maximum: int | None = None
            basis = "subject-name row"
            plausible_max = {20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 100, 125, 150, 200}
            usable = [value for value in numbers if value <= 200]
            maxima = [value for value in usable if value in plausible_max]
            if maxima:
                candidate_max = max(maxima)
                lower = [value for value in usable if 0 < value < candidate_max]
                if lower:
                    # Subject codes and totals can both be present; the largest
                    # value below a printed maximum is the obtained mark.
                    obtained, maximum = max(lower), candidate_max
            words = number_words_value(line)
            if obtained is None and words is not None:
                obtained = words
                basis = "marks printed in words"
            if obtained is None:
                continue
            results.append(
                Candidate(
                    label=f"{subject} marks",
                    value=f"{obtained}" + (f" out of {maximum}" if maximum is not None else ""),
                    page=page.page_number,
                    document_id=page.document_id,
                    document_name=page.document_name,
                    confidence=page.confidence,
                    basis=basis + f"; obtained={obtained}" + (f", maximum={maximum}" if maximum is not None else ", maximum not clearly present"),
                )
            )
    return results


def extract_total(pages: Iterable[Page]) -> tuple[Candidate | None, Candidate | None]:
    total: Candidate | None = None
    percent: Candidate | None = None
    for page in pages:
        if total is None:
            for match in TOTAL_LABEL.finditer(page.text):
                total = Candidate(
                    label="total marks",
                    value=f"{match.group(1)} out of {match.group(2)}",
                    page=page.page_number,
                    document_id=page.document_id,
                    document_name=page.document_name,
                    confidence=page.confidence,
                    basis="explicit total label with maximum",
                )
                break
        if percent is None:
            match = PERCENT_LABEL.search(page.text)
            if match:
                percent = Candidate(
                    label="percentage",
                    value=f"{match.group(1)}%",
                    page=page.page_number,
                    document_id=page.document_id,
                    document_name=page.document_name,
                    confidence=page.confidence,
                    basis="explicit percentage printed on the page",
                )
    return total, percent


def document_types(document_name: str, pages: Iterable[Page]) -> set[str]:
    folded = (document_name or "").casefold()
    types: set[str] = set()
    if re.search(r"\bpan\b|permanent account", folded):
        types.add("pan")
    if re.search(r"aadha?ar|adhar|aashar|uidai", folded):
        types.add("aadhaar")
    if re.search(r"marksheet|mark sheet|marks statement|12th|class 12|result", folded):
        types.add("marksheet")
    if re.search(r"migration|transfer certificate", folded):
        types.add("migration")
    text = "\n".join(page.text for page in pages)
    if PAN_LABEL.search(text) and PAN_PATTERN.search(text.upper()):
        types.add("pan")
    if AADHAAR_LABEL.search(text):
        types.add("aadhaar")
    if BOARD_NAME_HINT.search(text) or re.search(r"\b(?:marks|subject|maximum|obtained)\b", text, re.I):
        if re.search(r"\b(?:examination|marks|subject|result)\b", text, re.I):
            types.add("marksheet")
    if re.search(r"migration\s+certificate", text, re.I):
        types.add("migration")
    return types


def _requested_types(query: str) -> set[str]:
    folded = (query or "").casefold()
    types: set[str] = set()
    if re.search(r"\bpan\b|permanent account", folded):
        types.add("pan")
    if re.search(r"aadha?ar|adhar|aashar|\buid\b|uidai", folded):
        types.add("aadhaar")
    if re.search(r"board|marksheet|mark sheet|marks statement|12th|class 12|exam|result|subject|maths|mathematics|percentage|total marks", folded):
        types.add("marksheet")
    if re.search(r"migration|transfer certificate", folded):
        types.add("migration")
    if re.search(r"father|mother|parent", folded):
        types |= {"pan", "aadhaar", "marksheet", "migration"}
    return types


def _requested_subject(query: str) -> str | None:
    match = SUBJECT_QUERY.search(query or "")
    if not match:
        return None
    name = match.group(1).casefold()
    if name == "maths":
        return "mathematics"
    if name == "math":
        return "mathematics"
    return name


def _dedupe_candidates(candidates: Iterable[Candidate], key_attr: str = "value") -> list[Candidate]:
    """Merges duplicates of the same value, keeping the strongest evidence."""
    merged: dict[str, Candidate] = {}
    for candidate in candidates:
        key = re.sub(r"[\s-]", "", str(getattr(candidate, key_attr))).casefold()
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        if (candidate.confidence or 0) > (existing.confidence or 0):
            merged[key] = candidate
    return list(merged.values())


def _citation(candidate: Candidate, snippet: str) -> dict[str, Any]:
    return {
        "document_id": candidate.document_id,
        "document_name": candidate.document_name,
        "page_number": candidate.page,
        "snippet": snippet,
        "confidence": candidate.confidence,
    }


def _cluster_ocr_variants(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Groups name readings that only differ by plausible OCR substitutions."""
    clusters: list[list[Candidate]] = []
    for candidate in candidates:
        for cluster in clusters:
            if query_understanding.names_are_ocr_variants(cluster[0].value, candidate.value):
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])
    return clusters


def _field_state(candidates: list[Candidate], *, unique_threshold: float = 70.0) -> str:
    if not candidates:
        return "not_found"
    if len(candidates) > 1:
        return "ambiguous"
    candidate = candidates[0]
    if candidate.confidence is None or candidate.confidence < unique_threshold:
        return "ambiguous"
    return "confirmed"


def _verification_line(candidate: Candidate) -> str:
    if candidate.confidence is None:
        return "The page quality is too low to verify this value."
    return f"The page was read by OCR at about {round(candidate.confidence)}% confidence, so verify it visually."


def _answer_candidate(
    query: str,
    field_label: str,
    candidates: list[Candidate],
    *,
    page_hint: str,
    show_label: str | None = None,
    threshold: float = 70.0,
    reveal_low_confidence: bool = False,
) -> dict[str, Any] | None:
    unique = _dedupe_candidates(candidates)
    if not unique:
        return None
    if len(unique) > 1:
        pages = []
        seen = set()
        for item in unique[:4]:
            key = (item.document_id, item.page)
            if key in seen:
                continue
            seen.add(key)
            pages.append(
                f"- {item.document_name}" + (f", page {item.page}" if item.page else "")
                + (f" (possible value: {item.value})" if show_label else "")
            )
        clusters = (
            _cluster_ocr_variants(unique) if "name" in field_label else [[item] for item in unique]
        )
        if len(clusters) == 1:
            readings = " / ".join(f'"{item.value}"' for item in unique[:3])
            answer = (
                f"I found more than one possible reading of your {field_label} in your documents. "
                f"They look like OCR variants of the same value: {readings}. "
                "The letters differ only slightly, which usually means OCR misread a character; "
                "check the original page before relying on the spelling.\n\n"
                + "\n".join(pages)
            )
            return {
                "state": "ambiguous",
                "field": field_label,
                "answer": answer,
                "candidates": unique[:4],
                "citations": unique[:4],
                "ocr_variants": True,
            }
        answer = (
            f"I found more than one possible {field_label} in your documents, so I can't safely confirm "
            f"which one is correct. Please verify it in the original document.\n\n"
            + "\n".join(pages)
        )
        return {
            "state": "ambiguous",
            "field": field_label,
            "answer": answer,
            "candidates": unique[:4],
            "citations": unique[:4],
        }
    candidate = unique[0]
    if candidate.confidence is not None and candidate.confidence < threshold:
        shown = f" It appears to be \"{candidate.value}\"." if reveal_low_confidence else ""
        location = f"page {candidate.page} of " if candidate.page else ""
        answer = (
            f"I found a possible {field_label} on {location}\"{candidate.document_name}\", "
            f"but it isn't clear enough to confirm.{shown} {_verification_line(candidate)}"
        )
        return {
            "state": "ambiguous",
            "field": field_label,
            "answer": answer,
            "candidates": [candidate],
            "citations": [candidate],
        }
    location = f"\nPage: {candidate.page}" if candidate.page else ""
    answer = f"Your {field_label} is {candidate.value}.\n\nSource: {candidate.document_name}{location}"
    return {
        "state": "confirmed",
        "field": field_label,
        "value": candidate.value,
        "answer": answer,
        "candidates": [candidate],
        "citations": [candidate],
    }


def _pan_summary(pages: list[Page], doc_name: str) -> dict[str, Any]:
    pan = _dedupe_candidates(extract_pan(pages))
    names = _dedupe_candidates(extract_names(pages, "name"))
    fathers = _dedupe_candidates(extract_names(pages, "father's name"))
    dobs = _dedupe_candidates(extract_dob(pages))
    lines = [f"Here is what I can read from \"{doc_name}\":"]
    citations: list[Candidate] = []
    unreadable: list[str] = []
    if pan:
        for item in pan[:1]:
            lines.append(f"- PAN number: {item.value}" + (f" (page {item.page})" if item.page else ""))
            citations.append(item)
    elif any(PAN_LABEL.search(page.text) for page in pages):
        pan_page = _best_label_page(pages, PAN_LABEL)
        unreadable.append("PAN number: the field is present but the characters are not clear enough to read reliably.")
        citations.append(pan_page)
    else:
        lines.append("- PAN number: not printed on the readable pages.")
    if names:
        item = names[0]
        suffix = " — OCR spelling may be uncertain" if (item.confidence or 0) < 75 else ""
        variants = [
            candidate.value
            for candidate in names[1:]
            if query_understanding.names_are_ocr_variants(item.value, candidate.value)
        ]
        if variants:
            suffix += f" (also read as {' / '.join(variants[:2])}; likely the same value)"
        lines.append(f"- Name: {item.value} (page {item.page}){suffix}")
        citations.append(item)
    if fathers:
        item = fathers[0]
        suffix = " — OCR spelling may be uncertain" if (item.confidence or 0) < 75 else ""
        variants = [
            candidate.value
            for candidate in fathers[1:]
            if query_understanding.names_are_ocr_variants(item.value, candidate.value)
        ]
        if variants:
            suffix += f" (also read as {' / '.join(variants[:2])}; likely the same value)"
        lines.append(f"- Father's name: {item.value} (page {item.page}){suffix}")
        citations.append(item)
    if dobs:
        item = dobs[0]
        lines.append(f"- Date of birth: {item.value} (page {item.page})")
        citations.append(item)
    for item in unreadable:
        lines.append(f"- {item}")
    if unreadable and not (pan or names or fathers or dobs):
        lines.append("- No other labelled fields on the readable PAN pages were clear enough to quote.")
    if unreadable:
        lines.append("")
        lines.append("Open the original page to verify the unreadable field. Retry processing may help if the scan is poor.")
    if not citations and not unreadable:
        return {
            "state": "unreadable",
            "field": "PAN card information",
            "answer": f"I found \"{doc_name}\", but I couldn't read any reliable fields from it.",
            "citations": [],
        }
    unique_citations = _dedupe_candidates(citations, key_attr="page")
    return {
        "state": "ambiguous" if unreadable else "confirmed",
        "field": "PAN card information",
        "answer": "\n".join(lines),
        "citations": unique_citations,
    }


def answer_document_question(
    query: str,
    pages: list[Page],
    *,
    incomplete_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Answers a supported document question from full stored page text.

    Returns None when the question is not one this deterministic layer covers,
    so the caller can fall back to the existing model path.
    """
    if not pages:
        return None
    folded = (query or "").casefold()
    requested_types = _requested_types(query)
    # Documents were already selected by type in documents.answer_document_question;
    # keep every readable page of those documents so fields split across pages
    # (front/back, name and number) are all available.
    scope = pages
    grouped: dict[str, list[Page]] = {}
    for page in scope:
        grouped.setdefault(page.document_id, []).append(page)
    primary_doc = max(grouped.values(), key=len) if grouped else []
    document_name = primary_doc[0].document_name if primary_doc else "your document"

    wants_information = bool(re.search(
        r"what (?:information|details|is written|does .* say)|tell me about|everything|"
        r"what(?:'s| is) (?:on|written on)|summari[sz]e|show me my|show my|details of",
        folded,
    ))
    wants_pan = bool(re.search(r"\bpan\b|permanent account number", folded))
    wants_aadhaar = bool(re.search(r"aadha?ar|adhar|aashar|\buid\b|uidai", folded))
    wants_dob = bool(re.search(r"\b(?:dob|date of birth|birth date|born)\b", folded))
    wants_board = bool(re.search(r"\bboard\b|which board|education board", folded))
    wants_subject = _requested_subject(query) if re.search(r"marks|score|percentage|result|grade", folded) else None
    wants_father = bool(re.search(r"father", folded))
    wants_mother = bool(re.search(r"mother", folded))
    wants_total = bool(re.search(r"total|percentage|percent|aggregate", folded))

    if wants_pan and wants_information:
        pan_pages = [page for page in scope if PAN_LABEL.search(page.text)]
        return _pan_summary(pan_pages or primary_doc or scope, document_name)
    if wants_pan:
        result = _answer_candidate(query, "PAN number", extract_pan(scope), page_hint="")
        if result:
            return result
        if any(PAN_LABEL.search(page.text) for page in scope):
            return _unreadable("PAN number", scope, PAN_LABEL)
    if wants_aadhaar:
        result = _answer_candidate(query, "Aadhaar number", extract_aadhaar(scope), page_hint="")
        if result:
            return result
        if any(AADHAAR_LABEL.search(page.text) for page in scope):
            return _unreadable("Aadhaar number", scope, AADHAAR_LABEL)
    if wants_dob:
        result = _answer_candidate(query, "date of birth", extract_dob(scope), page_hint="")
        if result:
            return result
        if any(DOB_LABEL.search(page.text) for page in scope):
            return _unreadable("date of birth", scope, DOB_LABEL)
    if wants_board:
        candidates = _dedupe_candidates(extract_board(scope))
        if candidates:
            result = _answer_candidate(
                query, "education board", candidates, page_hint="", reveal_low_confidence=True
            )
            if result:
                result["answer"] = result["answer"].replace("Your education board is", "You passed from")
                return result
        # If a board isn't explicitly printed, do not infer one from an acronym.
        if incomplete_documents:
            return {
                "state": "incomplete",
                "field": "education board",
                "answer": "This marksheet has pages that haven't been processed completely, so I can't confirm the board yet.",
                "citations": [],
            }
        return {
            "state": "not_found",
            "field": "education board",
            "answer": "I couldn't find an explicitly printed education board on your marksheet.",
            "citations": [],
        }
    if wants_subject:
        candidates = _dedupe_candidates(extract_subject_rows(scope, wants_subject))
        if candidates:
            item = candidates[0]
            line = f"You got {item.value} in {wants_subject.title()}."
            if "out of" in item.value:
                obtained, maximum = item.value.split(" out of ")
                try:
                    percent = round(int(obtained) / int(maximum) * 100, 1)
                    line += f" That is {percent}% based on the maximum printed on that row."
                except (ValueError, ZeroDivisionError):
                    pass
            else:
                line += " The maximum marks for that row were not clearly readable, so I haven't calculated a percentage."
            if (item.confidence or 0) < 70:
                line += " This row was read by OCR and should be checked against the original marksheet."
            return {
                "state": "confirmed" if item.confidence is None or item.confidence >= 70 else "ambiguous",
                "field": f"{wants_subject.title()} marks",
                "value": item.value,
                "answer": line + f"\n\nSource: {item.document_name}\nPage: {item.page}",
                "citations": [item],
            }
        return {
            "state": "not_found" if not incomplete_documents else "incomplete",
            "field": f"{wants_subject.title()} marks",
            "answer": (
                f"I couldn't find a clearly readable {wants_subject.title()} marks row in your marksheet."
                if not incomplete_documents
                else "The marksheet hasn't been fully processed, so I can't confirm that subject row yet."
            ),
            "citations": [],
        }
    if wants_father or wants_mother:
        label = "father's name" if wants_father else "mother's name"
        candidates = _dedupe_candidates(extract_names(scope, label))
        result = _answer_candidate(
            query, label, candidates, page_hint="", show_label=True, threshold=75.0
        )
        if result:
            if (
                result["state"] == "ambiguous"
                and len(result.get("candidates") or []) == 1
            ):
                candidate = result["candidates"][0]
                if candidate.confidence is not None and candidate.confidence < 75:
                    result["answer"] = (
                        f"The {label} appears to be \"{candidate.value}\" on page {candidate.page}, "
                        "but the OCR spelling isn't reliable enough to treat it as verified. "
                        "Please check the original page."
                    )
            return result
        if any((FATHER_LABEL if wants_father else MOTHER_LABEL).search(page.text) for page in scope):
            return _unreadable(label, scope, FATHER_LABEL if wants_father else MOTHER_LABEL)
        return {
            "state": "not_found",
            "field": label,
            "answer": f"I couldn't find your {label} in the processed pages.",
            "citations": [],
        }
    if wants_total:
        total, percent = extract_total(scope)
        if total or percent:
            parts = []
            citations = []
            if total:
                parts.append(f"Total marks: {total.value} (page {total.page})")
                citations.append(total)
            if percent:
                parts.append(f"Percentage: {percent.value} (page {percent.page})")
                citations.append(percent)
            return {
                "state": "confirmed",
                "field": "total marks",
                "answer": "\n".join(parts) + f"\n\nSource: {citations[0].document_name}",
                "citations": citations,
            }
    return None


def _best_label_page(pages: list[Page], label: re.Pattern[str]) -> Candidate:
    best_page = max(pages, key=lambda page: (len(label.findall(page.text)), -page.page_number))
    return Candidate(
        label="unreadable field",
        value="",
        page=best_page.page_number,
        document_id=best_page.document_id,
        document_name=best_page.document_name,
        confidence=best_page.confidence,
        basis="label present without a trustworthy value",
    )


def _unreadable(
    field_label: str,
    pages: list[Page],
    label: re.Pattern[str] | None = None,
) -> dict[str, Any]:
    page = _best_label_page(pages, label) if label is not None else Candidate(
        label=field_label,
        value="",
        page=pages[0].page_number,
        document_id=pages[0].document_id,
        document_name=pages[0].document_name,
        confidence=pages[0].confidence,
    )
    location = f"page {page.page} of \"{page.document_name}\"" if page.page else f"\"{page.document_name}\""
    return {
        "state": "unreadable",
        "field": field_label,
        "answer": (
            f"I found the {field_label} field on {location}, but it isn't clear enough to read reliably. "
            "Please inspect the original page."
        ),
        "citations": [page],
    }
