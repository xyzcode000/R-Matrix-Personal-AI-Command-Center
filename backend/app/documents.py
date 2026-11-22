import asyncio
import csv
import hashlib
import io
import math
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from docx import Document as WordDocument
from docx.opc.exceptions import PackageNotFoundError
from fastapi import HTTPException, UploadFile
from lxml.etree import XMLSyntaxError
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError
from xml.etree.ElementTree import ParseError

from . import crypto, document_fields, document_pipeline, query_understanding
from .config import settings
from .database import utc_now
from .document_pipeline import (
    DocumentProcessingError,
    ExtractionOutcome,
    PageResult,
    SUPPORTED_FORMAT_MESSAGE,
)
from .schemas import DocumentSearchRequest

ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".docx", ".doc", ".csv", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
}
IMAGE_EXTENSIONS = document_pipeline.IMAGE_EXTENSIONS
DOCUMENT_CATEGORIES = (
    "Identity", "Education", "Financial", "Employment", "Medical", "Travel", "Personal", "Other",
)
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

PROCESSING_STATUSES = {
    "pending", "validating", "processing", "text_extracting", "ocr_processing", "indexing",
    "indexed", "partial", "failed", "retry_required",
}
READY_STATUSES = {"indexed", "partial"}

_PROCESSING_SEMAPHORE = threading.Semaphore(1)
_DOCUMENT_LOCKS: dict[str, threading.Lock] = {}
_DOCUMENT_LOCKS_GUARD = threading.Lock()


def normalize_category(value: str | None) -> str:
    cleaned = (value or "").strip().casefold()
    for category in DOCUMENT_CATEGORIES:
        if category.casefold() == cleaned:
            return category
    return "Other"


def _clean_display_name(value: str | None, fallback: str) -> str:
    name = " ".join((value or "").split())
    if not name:
        # Older clients send only the file; keep their original filename as the name.
        name = fallback
    if len(name) > 120:
        raise HTTPException(status_code=400, detail="A document name must be 120 characters or fewer.")
    return name


def _clean_description(value: str | None) -> str:
    description = " ".join((value or "").split())
    if len(description) > 600:
        raise HTTPException(status_code=400, detail="A document description must be 600 characters or fewer.")
    return description


def _document_mime(extension: str, supplied: str | None = None) -> str:
    fallback = MIME_TYPES.get(extension, "application/octet-stream")
    if supplied:
        media_type = supplied.split(";", 1)[0].strip().lower()
        if media_type and media_type not in GENERIC_CONTENT_TYPES:
            return supplied.split(";", 1)[0].strip()
    return fallback.split(";", 1)[0].strip()


def _seal(content: bytes) -> bytes:
    return crypto.encrypt_bytes(content) if crypto.encryption_enabled() else content


def _open(content: bytes) -> bytes:
    return crypto.decrypt_bytes(content)


SEARCH_STOP_WORDS = {
    "a", "about", "after", "also", "am", "an", "and", "are", "as", "at", "be", "been",
    "before", "being", "between", "by", "can", "compare", "could", "describe", "did",
    "do", "does", "doing", "explain", "extract", "for", "from", "had", "has", "have",
    "having", "he", "her", "here", "how", "i", "if", "in", "into", "is", "it", "its",
    "main", "me", "mine", "more", "my", "not", "of", "on", "or", "our", "ours", "out",
    "over", "please", "search", "she", "should", "show", "so", "some", "such",
    "summarize", "summary", "tell", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "through", "to", "too", "under", "us",
    "very", "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "why", "will", "with", "would", "you", "your", "yours",
}
GENERIC_CONTENT_TYPES = {"application/octet-stream", "binary/octet-stream"}

# Common Indian document names and spelling variants. These are query concepts,
# not document-authoritative facts; they only help retrieval find the right file.
QUERY_SYNONYMS = {
    "about": {"profile", "background", "overview"},
    "aadhaar": {"aadhar", "adhar", "aashar", "aashaar", "uid", "uidai", "identity"},
    "aadhar": {"aadhaar", "adhar", "aashar", "uid", "uidai", "identity"},
    "aashar": {"aadhaar", "aadhar", "adhar", "uid"},
    "adhar": {"aadhaar", "aadhar", "aashar", "uid", "uidai"},
    "background": {"profile", "about", "overview"},
    "based": {"location", "home", "studying", "study", "origin"},
    "birthday": {"born", "birth", "age", "dob"},
    "career": {"goal", "direction", "path", "role", "trajectory"},
    "campus": {"college", "university", "school", "education"},
    "certificate": {"certification", "marksheet", "migration", "transfer", "passing"},
    "college": {"education", "university", "school", "degree", "course", "studying", "campus"},
    "computer": {"laptop", "hardware", "machine", "device"},
    "dl": {"licence", "license", "driving"},
    "dob": {"birth", "born", "birthday", "date"},
    "driving": {"licence", "license", "dl", "vehicle"},
    "education": {"study", "studying", "degree", "learning", "training", "college", "university", "school", "course"},
    "friend": {"friends", "buddy"},
    "friends": {"friend", "buddy"},
    "goal": {"career", "direction", "path", "role"},
    "hardware": {"laptop", "computer", "device", "processor", "gpu", "ram"},
    "identity": {"aadhaar", "aadhar", "pan", "licence", "license", "passport"},
    "laptop": {"computer", "hardware", "machine", "device", "processor", "gpu", "ram"},
    "learn": {"learning", "studying", "skill", "skills", "training"},
    "learning": {"learn", "studying", "skill", "skills", "training"},
    "licence": {"license", "driving", "dl"},
    "license": {"licence", "driving", "dl"},
    "limit": {"limitation", "limitations", "constraint", "constraints", "concern", "concerns"},
    "location": {"based", "home", "origin", "study", "studying"},
    "machine": {"laptop", "computer", "hardware", "device"},
    "marks": {"marksheet", "mark", "score", "grade", "result", "percentage"},
    "marksheet": {"marks", "mark", "statement", "score", "grade", "result", "percentage"},
    "matrix": {"project", "architecture", "platform", "application", "system"},
    "migration": {"transfer", "certificate", "tc", "migration"},
    "myself": {"profile", "personal", "background", "overview", "identity"},
    "pan": {"pan", "card", "permanent", "account", "number", "tax"},
    "passport": {"travel", "identity"},
    "profile": {"about", "background", "overview", "identity"},
    "project": {"matrix", "architecture", "system", "application"},
    "result": {"marks", "marksheet", "score", "grade"},
    "school": {"college", "university", "education", "studying", "course"},
    "score": {"marks", "marksheet", "result", "grade", "percentage"},
    "maths": {"mathematics", "math"},
    "mathematics": {"maths", "math"},
    "board": {"council", "university", "examination"},
    "father": {"father's", "parent", "guardian"},
    "mother": {"mother's", "parent", "guardian"},
    "self": {"profile", "personal", "background", "overview", "identity"},
    "skill": {"skills", "technology", "technologies", "stack", "tool", "tools"},
    "skills": {"skill", "technology", "technologies", "stack", "tool", "tools"},
    "stack": {"technology", "technologies", "framework", "tools"},
    "study": {"studying", "education", "degree", "learning", "college", "university", "course"},
    "studying": {"study", "education", "degree", "learning", "college", "university", "course"},
    "tax": {"pan", "income"},
    "technology": {"technologies", "stack", "framework", "tools", "skill", "skills"},
    "technologies": {"technology", "stack", "framework", "tools", "skill", "skills"},
    "transfer": {"migration", "certificate", "tc"},
    "uid": {"aadhaar", "aadhar", "uidai", "identity"},
    "uidai": {"aadhaar", "aadhar", "uid"},
    "university": {"college", "school", "education", "degree", "course", "campus"},
}


def _safe_display_name(filename: str | None) -> tuple[str, str]:
    if not filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="A valid filename is required.")
    name = Path(filename.replace("\\", "/")).name
    extension = Path(name).suffix.lower()
    if not name or name in {".", ".."} or extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=SUPPORTED_FORMAT_MESSAGE)
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Filename must be 255 characters or fewer.")
    return name, extension


def _validate_content_type(extension: str, content_type: str | None) -> None:
    if not content_type:
        raise HTTPException(status_code=415, detail="A content type is required for the uploaded file.")
    media_type = content_type.split(";", 1)[0].strip().lower()
    allowed = {
        ".pdf": {"application/pdf", *GENERIC_CONTENT_TYPES},
        ".txt": {"text/plain", *GENERIC_CONTENT_TYPES},
        ".md": {"text/markdown", "text/x-markdown", "text/plain", *GENERIC_CONTENT_TYPES},
        ".docx": {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
            *GENERIC_CONTENT_TYPES,
        },
        ".doc": {"application/msword", "application/vnd.ms-office", *GENERIC_CONTENT_TYPES},
        ".csv": {"text/csv", "application/csv", "text/plain", *GENERIC_CONTENT_TYPES},
        ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/zip", *GENERIC_CONTENT_TYPES},
        ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/zip", *GENERIC_CONTENT_TYPES},
        ".png": {"image/png", *GENERIC_CONTENT_TYPES},
        ".jpg": {"image/jpeg", *GENERIC_CONTENT_TYPES},
        ".jpeg": {"image/jpeg", *GENERIC_CONTENT_TYPES},
        ".webp": {"image/webp", *GENERIC_CONTENT_TYPES},
        ".bmp": {"image/bmp", "image/x-ms-bmp", *GENERIC_CONTENT_TYPES},
        ".tif": {"image/tiff", *GENERIC_CONTENT_TYPES},
        ".tiff": {"image/tiff", *GENERIC_CONTENT_TYPES},
    }
    if media_type not in allowed[extension]:
        raise HTTPException(status_code=415, detail="The uploaded content type does not match its file extension.")


def _markdown_sections(text: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            if any(part.strip() for part in lines):
                sections.append((heading, lines))
            heading = match.group(1).strip()
            lines = []
        else:
            lines.append(line)
    if any(part.strip() for part in lines):
        sections.append((heading, lines))
    if not sections and text.strip():
        sections.append((None, text.splitlines()))
    return [(title, "\n".join(parts)) for title, parts in sections]


def _check_zip_container(content: bytes, required_entry: str, kind: str) -> None:
    if not content.startswith(b"PK"):
        raise HTTPException(status_code=400, detail=f"The uploaded file is not a valid {kind} document.")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            expanded_size = sum(entry.file_size for entry in entries)
            if (
                required_entry not in archive.namelist()
                or len(entries) > 2000
                or expanded_size > min(settings.max_upload_bytes * 20, 100 * 1024 * 1024)
            ):
                raise HTTPException(status_code=400, detail=f"The {kind} file is invalid or too large to extract safely.")
    except BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"The {kind} file is corrupt.") from exc


def _validate_file_signature(extension: str, content: bytes) -> None:
    if extension == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.")
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise HTTPException(status_code=400, detail="Encrypted PDFs are not supported.")
            if not reader.pages:
                raise HTTPException(status_code=400, detail="The PDF contains no pages.")
        except HTTPException:
            raise
        except (PdfReadError, FileNotDecryptedError, ValueError, OSError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="The PDF could not be read.") from exc
    elif extension == ".doc":
        if not content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise HTTPException(status_code=400, detail="The uploaded file is not a valid legacy DOC document.")
    elif extension == ".docx":
        _check_zip_container(content, "word/document.xml", "DOCX")
    elif extension == ".xlsx":
        _check_zip_container(content, "xl/workbook.xml", "XLSX")
    elif extension == ".pptx":
        _check_zip_container(content, "ppt/presentation.xml", "PPTX")
    elif extension == ".png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=400, detail="The image content does not match its file extension.")
    elif extension in {".jpg", ".jpeg"} and not content.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=400, detail="The image content does not match its file extension.")
    elif extension == ".webp" and not (
        len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    ):
        raise HTTPException(status_code=400, detail="The image content does not match its file extension.")
    elif extension == ".bmp" and not content.startswith(b"BM"):
        raise HTTPException(status_code=400, detail="The image content does not match its file extension.")
    elif extension in {".tif", ".tiff"} and content[:4] not in {b"II*\x00", b"MM\x00*"}:
        raise HTTPException(status_code=400, detail="The image content does not match its file extension.")
    elif extension in {".txt", ".md", ".csv"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Text files must use UTF-8 encoding.") from exc
        if "\x00" in text:
            raise HTTPException(status_code=400, detail="The text file contains unsupported binary data.")


def _extract_legacy_doc(content: bytes) -> list[tuple[int | None, str, str | None]]:
    import shutil

    antiword = shutil.which("antiword")
    if antiword is None:
        raise HTTPException(
            status_code=503,
            detail="Legacy DOC extraction requires Antiword. Install Antiword and ensure it is on PATH, or save the document as DOCX.",
        )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        completed = subprocess.run(
            [antiword, str(temporary_path)],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=422, detail="Legacy DOC extraction exceeded its 15-second limit.") from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail="Legacy DOC extraction could not start Antiword.") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise HTTPException(status_code=400, detail="The legacy DOC file could not be read by Antiword.")
    try:
        text = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="The legacy DOC text could not be decoded.") from exc
    if not text.strip():
        raise HTTPException(status_code=400, detail="No readable text was found in the legacy DOC file.")
    return [(None, text, None)]


def _extract_office_document(extension: str, content: bytes) -> tuple[list[tuple[int | None, str, str | None]], str]:
    """Non-page-based formats: DOCX, DOC, XLSX, PPTX, CSV, TXT and Markdown."""
    if extension == ".doc":
        if not content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise HTTPException(status_code=400, detail="The uploaded file is not a valid legacy DOC document.")
        return _extract_legacy_doc(content), "antiword"

    try:
        if extension == ".docx":
            _check_zip_container(content, "word/document.xml", "DOCX")
            document = WordDocument(io.BytesIO(content))
            sections: list[tuple[str | None, list[str]]] = []
            heading: str | None = None
            lines: list[str] = []
            for paragraph in document.paragraphs:
                text = paragraph.text.strip()
                if not text:
                    continue
                if paragraph.style.name.startswith("Heading"):
                    if lines:
                        sections.append((heading, lines))
                    heading, lines = text, []
                else:
                    lines.append(text)
            for table in document.tables:
                for row in table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                    if row_text:
                        lines.append(row_text)
            if lines:
                sections.append((heading, lines))
            extracted = [(None, "\n".join(parts), title) for title, parts in sections]
            method = "docx"
        elif extension == ".xlsx":
            _check_zip_container(content, "xl/workbook.xml", "XLSX")
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            extracted = []
            try:
                for sheet in workbook.worksheets:
                    rows = []
                    for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                        if row_index > 10000:
                            raise HTTPException(status_code=413, detail="Each spreadsheet sheet is limited to 10,000 rows.")
                        values = [str(value).strip() if value is not None else "" for value in row[:100]]
                        if any(values):
                            rows.append(f"Row {row_index}: " + " | ".join(f"Column {index + 1}: {value}" for index, value in enumerate(values) if value))
                    if rows:
                        extracted.append((None, "\n".join(rows), sheet.title))
            finally:
                workbook.close()
            method = "xlsx"
        elif extension == ".pptx":
            _check_zip_container(content, "ppt/presentation.xml", "PPTX")
            from pptx import Presentation

            presentation = Presentation(io.BytesIO(content))
            if len(presentation.slides) > 300:
                raise HTTPException(status_code=413, detail="Presentations are limited to 300 slides.")
            extracted = []
            for slide_number, slide in enumerate(presentation.slides, start=1):
                texts = []
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        text = shape.text.strip()
                        if text:
                            texts.append(text)
                    if getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                            if cells:
                                texts.append(" | ".join(cells))
                if texts:
                    title = texts[0][:180] if texts else f"Slide {slide_number}"
                    extracted.append((slide_number, "\n".join(texts), title))
            method = "pptx"
        elif extension == ".csv":
            text = content.decode("utf-8-sig")
            rows = []
            for row_number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
                if row_number > 10000:
                    raise HTTPException(status_code=413, detail="CSV files are limited to 10,000 rows.")
                if len(row) > 100:
                    raise HTTPException(status_code=413, detail="CSV files are limited to 100 columns.")
                if any(cell.strip() for cell in row):
                    rows.append(f"Row {row_number}: " + " | ".join(f"Column {index + 1}: {cell.strip()}" for index, cell in enumerate(row) if cell.strip()))
            extracted = [(None, "\n".join(rows), "CSV")]
            method = "csv"
        else:
            text = content.decode("utf-8-sig")
            if "\x00" in text:
                raise HTTPException(status_code=400, detail="The text file contains unsupported binary data.")
            extracted = [
                (None, section, heading)
                for heading, section in (_markdown_sections(text) if extension == ".md" else [(None, text)])
            ]
            method = "markdown" if extension == ".md" else "text"
    except HTTPException:
        raise
    except (
        UnicodeDecodeError,
        PackageNotFoundError,
        BadZipFile,
        XMLSyntaxError,
        ParseError,
        ValueError,
        OSError,
        KeyError,
        RuntimeError,
        csv.Error,
    ) as exc:
        message = (
            "Text files must use UTF-8 encoding."
            if extension in {".txt", ".md", ".csv"} and isinstance(exc, UnicodeDecodeError)
            else "The document could not be read safely."
        )
        raise HTTPException(status_code=400, detail=message) from exc

    if not extracted or not any(text.strip() for _, text, _ in extracted):
        raise HTTPException(status_code=400, detail="No readable text was found in the file.")
    return extracted, method


def extract_document_pages(
    extension: str,
    content: bytes,
    *,
    progress: document_pipeline.ProgressCallback | None = None,
) -> ExtractionOutcome:
    """Extracts every page, recording per-page status instead of all-or-nothing."""
    if extension == ".pdf":
        return document_pipeline.extract_pdf(content, progress=progress)
    if extension in IMAGE_EXTENSIONS:
        return document_pipeline.extract_image(extension, content, progress=progress)
    extracted, method = _extract_office_document(extension, content)
    pages = [
        PageResult(
            page_number=page_number,
            text=text.strip(),
            section_heading=_heading,
            method="native",
            status="ok" if text.strip() else "empty",
            error_category=None if text.strip() else "no_readable_text",
            error_detail=None if text.strip() else "No readable text was found in this section.",
        )
        for page_number, text, _heading in extracted
    ]
    usable = [page for page in pages if page.status == "ok"]
    if not usable:
        raise DocumentProcessingError("No readable text was found in the file.", category="no_readable_text")
    return ExtractionOutcome(
        pages=pages,
        detected_pages=len(pages),
        method=method,
        ocr_used=False,
    )


def _extract_document(extension: str, content: bytes) -> tuple[list[tuple[int | None, str, str | None]], str]:
    """Compatibility wrapper returning (page_number, text, heading) triples."""
    outcome = extract_document_pages(extension, content)
    extracted: list[tuple[int | None, str, str | None]] = []
    for page in outcome.pages:
        if page.status != "ok" or not page.text.strip():
            continue
        page_number = None if extension in {".txt", ".md", ".doc", ".docx", ".csv", ".xlsx"} else page.page_number
        extracted.append((page_number, page.text, None))
    return extracted, outcome.method


def _chunk_text(
    text: str,
    page_number: int | None,
    section_heading: str | None,
) -> list[tuple[int | None, str | None, str]]:
    size = settings.document_chunk_size
    overlap = settings.document_chunk_overlap
    chunks: list[tuple[int | None, str | None, str]] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window_start = start + size // 2
            # Prefer a line break so a field label and its value stay in the same
            # chunk; fall back to a word boundary for continuous prose.
            newline = text.rfind("\n", window_start, end)
            if newline > start:
                end = newline
            else:
                boundary = text.rfind(" ", window_start, end)
                if boundary > start:
                    end = boundary
        content = text[start:end].strip()
        if content:
            chunks.append((page_number, section_heading, content))
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < len(text) and text[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks


def _safe_storage_path(storage_name: str) -> Path:
    if not storage_name or Path(storage_name).name != storage_name or "/" in storage_name or "\\" in storage_name:
        raise HTTPException(status_code=500, detail="The stored document reference is invalid.")
    data_root = settings.data_dir.resolve()
    document_root = settings.documents_dir.resolve()
    target = (document_root / storage_name).resolve()
    if document_root.parent != data_root or target.parent != document_root:
        raise HTTPException(status_code=500, detail="The stored document reference is outside the data directory.")
    return target


def _chunk_rows(outcome_pages: list[PageResult]) -> list[tuple[str, int, int | None, str | None, str, str]]:
    rows: list[tuple[str, int, int | None, str | None, str, str]] = []
    index = 0
    for page in outcome_pages:
        if page.status != "ok" or not page.text.strip():
            continue
        for chunk_page, heading, content in _chunk_text(page.text, page.page_number, page.section_heading):
            rows.append(
                (
                    str(uuid4()),
                    index,
                    chunk_page,
                    heading,
                    content,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
            index += 1
    return rows


def _write_index(
    connection: sqlite3.Connection,
    document_id: str,
    extracted: list[tuple[int | None, str, str | None]],
) -> int:
    """Legacy-shaped index write used by tests and simple callers."""
    pages = [
        PageResult(page_number=page_number or 1, text=text, method="native", status="ok")
        for page_number, text, _ in extracted
        if text.strip()
    ]
    return _replace_index(connection, document_id, pages, processed_at=utc_now())


def _replace_index(
    connection: sqlite3.Connection,
    document_id: str,
    pages: list[PageResult],
    *,
    processed_at: str | None = None,
) -> int:
    """Replaces page rows and chunks atomically. Never appends duplicates."""
    connection.execute("DELETE FROM document_pages WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
    page_rows = [
        (
            str(uuid4()),
            document_id,
            page.page_number,
            page.text,
            page.section_heading,
            page.status,
            page.method,
            page.ocr_confidence,
            page.error_category,
            page.error_detail,
            page.duration_ms,
        )
        for page in pages
    ]
    connection.executemany(
        """INSERT INTO document_pages
           (id, document_id, page_number, content, section_heading, status, extraction_method,
            ocr_confidence, error_category, error_detail, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        page_rows,
    )
    chunks = _chunk_rows(pages)
    title_row = connection.execute(
        "SELECT COALESCE(display_name, original_name) AS name FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    detected_types = sorted(
        document_fields.document_types((title_row["name"] if title_row else "") or "", pages)
    )
    connection.execute(
        "UPDATE documents SET document_type = ? WHERE id = ?",
        (",".join(detected_types) or None, document_id),
    )
    connection.executemany(
        """INSERT INTO document_chunks
           (id, document_id, chunk_index, page_number, section_heading, content, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(row[0], document_id, *row[1:]) for row in chunks],
    )
    counts: dict[int | None, int] = {}
    for row in chunks:
        counts[row[2]] = counts.get(row[2], 0) + 1
    for page in pages:
        connection.execute(
            "UPDATE document_pages SET chunk_count = ? WHERE document_id = ? AND page_number IS ?",
            (counts.get(page.page_number, 0), document_id, page.page_number),
        )
    return len(chunks)


DOCUMENT_COLUMNS = (
    "id, original_name, display_name, description, category, mime_type, extension, size_bytes, "
    "page_count, status, created_at, content_hash, extracted_text_available, chunk_count, "
    "last_processed_at, processing_error, extraction_method, processed_pages, failed_pages, "
    "ocr_status, processing_stage, processing_attempts, last_attempt_at, document_type"
)


def _document_row(row: sqlite3.Row) -> dict[str, Any]:
    document = dict(row)
    document["extracted_text_available"] = bool(document["extracted_text_available"])
    document["display_name"] = document.get("display_name") or document.get("original_name") or "Document"
    document["category"] = document.get("category") or "Other"
    document["processed_pages"] = int(document.get("processed_pages") or 0)
    document["failed_pages"] = int(document.get("failed_pages") or 0)
    document["page_count"] = document.get("page_count")
    document["status"] = document.get("status") or "pending"
    document["document_types"] = [
        item for item in (document.get("document_type") or "").split(",") if item
    ]
    return document


def list_documents(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT {DOCUMENT_COLUMNS} FROM documents ORDER BY created_at DESC"
    ).fetchall()
    return [_document_row(row) for row in rows]


def library_documents(
    connection: sqlite3.Connection,
    *,
    query: str | None = None,
    category: str | None = None,
    extension: str | None = None,
    status_filter: str | None = None,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Owner-facing library listing with filters, sorting and pagination."""
    conditions: list[str] = []
    values: list[Any] = []
    term = (query or "").strip()
    if term:
        words = [word for word in re.split(r"\s+", term.casefold()) if word][:8] or [term.casefold()]
        for word in words:
            pattern = f"%{_escape_like(word)}%"
            synonyms = sorted(QUERY_SYNONYMS.get(word, set()))[:4]
            synonym_clause = ""
            synonym_values: list[str] = []
            for synonym in synonyms:
                synonym_clause += " OR lower(COALESCE(display_name, '')) LIKE ? ESCAPE '\\' OR lower(description) LIKE ? ESCAPE '\\'"
                synonym_values.extend([f"%{_escape_like(synonym)}%", f"%{_escape_like(synonym)}%"])
            conditions.append(
                "(lower(original_name) LIKE ? ESCAPE '\\' OR lower(COALESCE(display_name, '')) LIKE ? ESCAPE '\\' "
                "OR lower(description) LIKE ? ESCAPE '\\' OR lower(category) LIKE ? ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM document_pages p WHERE p.document_id = documents.id AND lower(p.content) LIKE ? ESCAPE '\\')"
                + synonym_clause
                + ")"
            )
            values.extend([pattern, pattern, pattern, pattern, pattern, *synonym_values])
    if category and category.casefold() not in {"", "all"}:
        conditions.append("category = ?")
        values.append(normalize_category(category))
    if extension and extension.casefold() not in {"", "all"}:
        conditions.append("extension = ?")
        values.append(extension if extension.startswith(".") else f".{extension}")
    if status_filter and status_filter.casefold() not in {"", "all"}:
        conditions.append("status = ?")
        values.append(status_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    ordering = {
        "name": "lower(COALESCE(display_name, original_name)) ASC",
        "name_desc": "lower(COALESCE(display_name, original_name)) DESC",
        "oldest": "created_at ASC",
        "newest": "created_at DESC",
        "pages": "COALESCE(page_count, 0) DESC, created_at DESC",
    }.get(sort, "created_at DESC")
    total = connection.execute(f"SELECT COUNT(*) FROM documents {where}", values).fetchone()[0]
    rows = connection.execute(
        f"SELECT {DOCUMENT_COLUMNS} FROM documents {where} ORDER BY {ordering} LIMIT ? OFFSET ?",
        [*values, max(1, min(limit, 200)), max(0, offset)],
    ).fetchall()
    facets = {
        "categories": [
            {"value": row[0], "count": row[1]}
            for row in connection.execute(
                "SELECT category, COUNT(*) FROM documents GROUP BY category ORDER BY category"
            ).fetchall()
        ],
        "extensions": [
            {"value": row[0], "count": row[1]}
            for row in connection.execute(
                "SELECT extension, COUNT(*) FROM documents GROUP BY extension ORDER BY extension"
            ).fetchall()
        ],
        "statuses": [
            {"value": row[0], "count": row[1]}
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM documents GROUP BY status ORDER BY status"
            ).fetchall()
        ],
    }
    return {
        "items": [_document_row(row) for row in rows],
        "total": int(total),
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
        "facets": facets,
    }


def get_document(connection: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    row = connection.execute(
        f"SELECT {DOCUMENT_COLUMNS}, storage_name FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    result = _document_row(row)
    storage_name = result.pop("storage_name", None)
    result["encryption"] = "unavailable"
    if storage_name:
        try:
            header = _safe_storage_path(storage_name).read_bytes()[:16]
            result["encryption"] = "aes-256-gcm" if crypto.is_encrypted(header) else "none"
        except (OSError, HTTPException):
            result["encryption"] = "missing"
    result["page_summary"] = _page_summary(connection, document_id)
    return result


def _page_summary(connection: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT page_number, status, extraction_method, ocr_confidence, error_category, chunk_count "
        "FROM document_pages WHERE document_id = ? ORDER BY page_number",
        (document_id,),
    ).fetchall()
    return {
        "total": len(rows),
        "processed": sum(1 for row in rows if row["status"] == "ok"),
        "failed": sum(1 for row in rows if row["status"] != "ok"),
        "ocr_pages": sum(1 for row in rows if row["extraction_method"] == "ocr"),
        "pages": [
            {
                "page_number": row["page_number"],
                "status": row["status"],
                "method": row["extraction_method"],
                "ocr_confidence": row["ocr_confidence"],
                "error_category": row["error_category"],
                "chunk_count": row["chunk_count"],
            }
            for row in rows
        ],
    }


def _document_lock(document_id: str) -> threading.Lock:
    with _DOCUMENT_LOCKS_GUARD:
        lock = _DOCUMENT_LOCKS.get(document_id)
        if lock is None:
            lock = threading.Lock()
            _DOCUMENT_LOCKS[document_id] = lock
        return lock


def _begin_job(connection: sqlite3.Connection, document_id: str, kind: str) -> str:
    attempts = connection.execute(
        "SELECT COUNT(*) FROM document_processing_jobs WHERE document_id = ?", (document_id,)
    ).fetchone()[0]
    job_id = str(uuid4())
    started_at = utc_now()
    connection.execute(
        """INSERT INTO document_processing_jobs
           (id, document_id, kind, status, stage, attempts, started_at)
           VALUES (?, ?, ?, 'running', 'validating', ?, ?)""",
        (job_id, document_id, kind, int(attempts) + 1, started_at),
    )
    connection.execute(
        """UPDATE documents SET status = 'processing', processing_stage = 'validating',
           processing_attempts = COALESCE(processing_attempts, 0) + 1, last_attempt_at = ?,
           processing_error = NULL WHERE id = ?""",
        (started_at, document_id),
    )
    connection.commit()
    return job_id


def _update_stage(
    connection: sqlite3.Connection,
    document_id: str,
    job_id: str,
    stage: str,
    done: int = 0,
    total: int = 0,
) -> None:
    try:
        connection.execute(
            "UPDATE documents SET processing_stage = ? WHERE id = ?", (stage, document_id)
        )
        connection.execute(
            "UPDATE document_processing_jobs SET stage = ?, pages_done = ?, pages_total = ? WHERE id = ?",
            (stage, done, total, job_id),
        )
        connection.commit()
    except sqlite3.Error:
        pass


def _finish_job(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    status: str,
    stage: str,
    pages_total: int = 0,
    pages_done: int = 0,
    error_category: str | None = None,
    error_detail: str | None = None,
) -> None:
    connection.execute(
        """UPDATE document_processing_jobs SET status = ?, stage = ?, pages_total = ?, pages_done = ?,
           error_category = ?, error_detail = ?, finished_at = ? WHERE id = ?""",
        (status, stage, pages_total, pages_done, error_category, error_detail, utc_now(), job_id),
    )


STATUS_MESSAGES = {
    "ocr_unavailable": (
        "OCR is unavailable. Scanned pages could not be read. "
        "Configure the local OCR engine and retry processing."
    ),
    "ocr_missing_language": "The OCR engine is missing required language data.",
    "pdf_password_protected": "Password-protected PDFs are not supported.",
    "no_readable_text": "No readable text was found in this document.",
}


def _status_for(outcome: ExtractionOutcome, chunk_count: int) -> str:
    if chunk_count <= 0:
        return "failed"
    failed = len(outcome.failed_pages)
    if failed == 0:
        return "indexed"
    if outcome.usable_pages:
        return "partial"
    return "failed"


def _ocr_status_for(outcome: ExtractionOutcome) -> str:
    ocr_pages = [page for page in outcome.pages if page.method == "ocr"]
    failures = [page for page in outcome.failed_pages if (page.error_category or "").startswith("ocr")]
    if failures and not ocr_pages:
        return "unavailable"
    if failures:
        return "partial"
    if ocr_pages:
        low = any((page.ocr_confidence or 100) < 60 for page in ocr_pages)
        return "low_confidence" if low else "used"
    return "not_required"


def process_document(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    content: bytes | None = None,
    rebuild_index_only: bool = False,
) -> dict[str, Any]:
    """Processes (or rebuilds) one document idempotently and recoverably."""
    lock = _document_lock(document_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="This document is already being processed.")
    job_id: str | None = None
    try:
        row = connection.execute(
            f"SELECT {DOCUMENT_COLUMNS}, storage_name FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        extension = row["extension"]
        previous_chunks = int(row["chunk_count"] or 0)
        kind = "rebuild_index" if rebuild_index_only else "process"
        job_id = _begin_job(connection, document_id, kind)
        _PROCESSING_SEMAPHORE.acquire()
        try:
            if rebuild_index_only:
                pages = _stored_pages_for_rebuild(connection, document_id)
                if not pages:
                    raise HTTPException(
                        status_code=409,
                        detail="No usable extracted pages are stored. Retry processing instead of rebuilding the index.",
                    )
                _update_stage(connection, document_id, job_id, "indexing", 0, len(pages))
                chunk_count = _replace_index(connection, document_id, pages)
                document_pipeline.mark_active(document_id, "indexing", len(pages), len(pages))
                outcome = None
                detection_total = int(row["page_count"] or len(pages))
                processed = len([page for page in pages if page.status == "ok"])
                failed = int(row["failed_pages"] or 0)
                method = row["extraction_method"] or "rebuilt"
                ocr_status = row["ocr_status"] or "not_required"
            else:
                if content is None:
                    target = _safe_storage_path(row["storage_name"])
                    if not target.exists():
                        raise HTTPException(status_code=404, detail="The stored original document file is missing.")
                    try:
                        content = _open(target.read_bytes())
                    except crypto.EncryptionUnavailable as exc:
                        raise HTTPException(status_code=503, detail=str(exc)) from exc
                _update_stage(connection, document_id, job_id, "text_extracting")

                def progress(stage: str, done: int, total: int) -> None:
                    _update_stage(connection, document_id, job_id, stage, done, total)
                    document_pipeline.mark_active(document_id, stage, done, total)

                document_pipeline.mark_active(document_id, "text_extracting", 0, 0)
                outcome = extract_document_pages(extension, content, progress=progress)
                _update_stage(
                    connection, document_id, job_id, "indexing",
                    len(outcome.usable_pages), outcome.detected_pages,
                )
                document_pipeline.mark_active(
                    document_id, "indexing", len(outcome.usable_pages), outcome.detected_pages
                )
                chunk_count = _replace_index(connection, document_id, outcome.pages)
                detection_total = outcome.detected_pages
                processed = len(outcome.usable_pages)
                failed = len(outcome.failed_pages)
                method = outcome.method
                ocr_status = _ocr_status_for(outcome)
            status = _status_for(outcome, chunk_count) if outcome is not None else (
                "partial" if int(row["failed_pages"] or 0) > 0 else "indexed"
            )
            if chunk_count <= 0 and outcome is not None:
                raise DocumentProcessingError(
                    "The extracted text did not produce any searchable chunks.",
                    category="index_empty",
                    retryable=False,
                )
            page_count = (
                detection_total
                if extension in {".pdf", ".pptx", *IMAGE_EXTENSIONS}
                else None
            )
            processed_at = utc_now()
            error_message = None
            warnings = outcome.warnings if outcome is not None else []
            if status == "partial":
                failed_pages = [page.page_number for page in outcome.failed_pages] if outcome else []
                readable = ", ".join(str(number) for number in failed_pages[:8])
                error_message = (
                    f"{failed} of {detection_total} pages need retry"
                    + (f" (pages {readable})." if readable else ".")
                )
            if warnings:
                error_message = " ".join(filter(None, [error_message, " ".join(warnings)]))
            connection.execute(
                """UPDATE documents SET page_count = ?, status = ?, extracted_text_available = ?,
                   chunk_count = ?, last_processed_at = ?, processing_error = ?, extraction_method = ?,
                   processed_pages = ?, failed_pages = ?, ocr_status = ?, processing_stage = NULL
                   WHERE id = ?""",
                (
                    page_count,
                    status,
                    1 if processed else 0,
                    chunk_count,
                    processed_at,
                    error_message,
                    method,
                    processed,
                    failed,
                    ocr_status,
                    document_id,
                ),
            )
            _finish_job(
                connection, job_id,
                status="succeeded" if status == "indexed" else status,
                stage="complete",
                pages_total=detection_total, pages_done=processed,
                error_category=None, error_detail=error_message,
            )
            connection.commit()
            return get_document(connection, document_id)
        except (HTTPException, DocumentProcessingError) as exc:
            retryable = isinstance(exc, DocumentProcessingError) and exc.retryable
            message = exc.detail if isinstance(exc, HTTPException) else exc.message
            category = exc.category if isinstance(exc, DocumentProcessingError) else _http_category(exc)
            retryable = retryable or (isinstance(exc, HTTPException) and exc.status_code in {502, 503, 504})
            _fail_document(
                connection,
                document_id,
                job_id,
                message=message,
                category=category,
                retryable=retryable,
                previous_chunks=previous_chunks,
                page_count=row["page_count"],
                processed_pages=row["processed_pages"],
                failed_pages=row["failed_pages"],
                pages=getattr(exc, "pages", None),
                detected_pages=getattr(exc, "detected_pages", None),
            )
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=exc.message) from exc
        except (OSError, sqlite3.Error) as exc:
            _fail_document(
                connection,
                document_id,
                job_id,
                message="The document could not be processed because of a local storage error.",
                category="storage_error",
                retryable=True,
                previous_chunks=previous_chunks,
                page_count=row["page_count"],
                processed_pages=row["processed_pages"],
                failed_pages=row["failed_pages"],
            )
            raise HTTPException(
                status_code=500, detail="The document could not be processed because of a local storage error."
            ) from exc
        finally:
            _PROCESSING_SEMAPHORE.release()
    finally:
        document_pipeline.clear_active(document_id)
        lock.release()


def _http_category(exc: HTTPException) -> str:
    if exc.status_code == 503:
        return "dependency_unavailable"
    if exc.status_code == 504:
        return "timeout"
    if exc.status_code == 413:
        return "limit_exceeded"
    return "unreadable_document"


def _fail_document(
    connection: sqlite3.Connection,
    document_id: str,
    job_id: str,
    *,
    message: str,
    category: str,
    retryable: bool,
    previous_chunks: int,
    page_count: Any,
    processed_pages: Any,
    failed_pages: Any,
    pages: list[PageResult] | None = None,
    detected_pages: int | None = None,
) -> None:
    status = "retry_required" if retryable else "failed"
    preserve = retryable and previous_chunks > 0
    if detected_pages:
        page_count = detected_pages
    if pages:
        processed_pages = len([page for page in pages if page.status == "ok" and page.text.strip()])
        failed_pages = len(pages) - processed_pages
    try:
        connection.rollback()
        if not preserve:
            connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM document_pages WHERE document_id = ?", (document_id,))
            if pages:
                # Page-level diagnostics are preserved even when nothing was readable.
                _replace_index(connection, document_id, pages)
        connection.execute(
            """UPDATE documents SET status = ?, processing_error = ?, last_processed_at = ?,
               extracted_text_available = ?, chunk_count = ?, processing_stage = NULL,
               processed_pages = ?, failed_pages = ? WHERE id = ?""",
            (
                status,
                message,
                utc_now(),
                1 if preserve else 0,
                previous_chunks if preserve else 0,
                processed_pages if preserve else 0,
                failed_pages if preserve else (page_count or 0),
                document_id,
            ),
        )
        _finish_job(
            connection, job_id, status=status, stage="failed",
            pages_total=int(page_count or 0), pages_done=int(processed_pages or 0),
            error_category=category, error_detail=message,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()


def _stored_pages_for_rebuild(connection: sqlite3.Connection, document_id: str) -> list[PageResult]:
    rows = connection.execute(
        """SELECT page_number, content, section_heading, extraction_method, ocr_confidence, status, duration_ms
           FROM document_pages WHERE document_id = ? AND status = 'ok' AND TRIM(content) <> ''
           ORDER BY page_number""",
        (document_id,),
    ).fetchall()
    return [
        PageResult(
            page_number=row["page_number"],
            text=row["content"],
            section_heading=row["section_heading"],
            method=row["extraction_method"] or "native",
            status="ok",
            ocr_confidence=row["ocr_confidence"],
            duration_ms=row["duration_ms"] or 0,
        )
        for row in rows
    ]


async def ingest_upload(
    upload: UploadFile,
    connection: sqlite3.Connection,
    *,
    display_name: str | None = None,
    description: str = "",
    category: str = "Other",
) -> dict[str, Any]:
    original_name, extension = _safe_display_name(upload.filename)
    title = _clean_display_name(display_name, original_name)
    description_text = _clean_description(description)
    category_value = normalize_category(category)
    mime_type = _document_mime(extension, upload.content_type)
    _validate_content_type(extension, upload.content_type)
    content = await upload.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        limit_mib = settings.max_upload_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the configured {limit_mib:g} MiB upload limit.",
        )
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    _validate_file_signature(extension, content)
    content_hash = hashlib.sha256(content).hexdigest()
    existing = connection.execute(
        "SELECT id FROM documents WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="This document is already indexed.")

    document_id = str(uuid4())
    storage_name = f"{document_id}{extension}"
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    target = _safe_storage_path(storage_name)
    created_at = utc_now()
    try:
        target.write_bytes(_seal(content))
        connection.execute(
            """INSERT INTO documents
            (id, original_name, display_name, description, category, mime_type, storage_name, extension,
             size_bytes, page_count, status, created_at, content_hash, extracted_text_available,
             chunk_count, last_processed_at, processing_error, extraction_method, processed_pages,
             failed_pages, ocr_status, processing_stage, processing_attempts, last_attempt_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?, 0, 0, NULL, NULL, NULL, 0, 0, NULL, NULL, 0, NULL)""",
            (
                document_id,
                original_name,
                title,
                description_text,
                category_value,
                mime_type,
                storage_name,
                extension,
                len(content),
                created_at,
                content_hash,
            ),
        )
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        target.unlink(missing_ok=True)
        if connection.execute(
            "SELECT 1 FROM documents WHERE content_hash = ?",
            (content_hash,),
        ).fetchone():
            raise HTTPException(status_code=409, detail="This document is already indexed.") from exc
        raise
    except (OSError, sqlite3.Error):
        connection.rollback()
        target.unlink(missing_ok=True)
        raise

    # Extraction runs off the event loop; the document row already exists so an
    # interrupted upload remains visible and recoverable in the library.
    try:
        return await asyncio.to_thread(process_document, connection, document_id, content=content)
    except HTTPException as exc:
        # Processing failures are recorded on the document (failed/retry_required)
        # and returned with the stored record; only genuine request errors raise.
        if exc.status_code in {400, 413, 422, 500, 503, 504}:
            return get_document(connection, document_id)
        raise


def reindex_document(connection: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    """Backwards-compatible alias: reprocess the original file end to end."""
    return process_document(connection, document_id)


def rebuild_document_index(connection: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    """Rebuilds chunks from already-verified extracted pages only."""
    return process_document(connection, document_id, rebuild_index_only=True)


def get_document_text(connection: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    document = get_document(connection, document_id)
    rows = connection.execute(
        """SELECT page_number, content, status, extraction_method, ocr_confidence, error_category, chunk_count
           FROM document_pages WHERE document_id = ? ORDER BY page_number, rowid""",
        (document_id,),
    ).fetchall()
    document["pages"] = [
        {
            "page_number": row["page_number"],
            "content": row["content"],
            "status": row["status"],
            "method": row["extraction_method"],
            "ocr_confidence": row["ocr_confidence"],
            "error_category": row["error_category"],
            "chunk_count": row["chunk_count"],
        }
        for row in rows
    ]
    return document


def read_document_file(connection: sqlite3.Connection, document_id: str) -> tuple[bytes, str, str]:
    """Returns (bytes, media_type, download_name) for the original upload.

    The stored file is decrypted in memory only; nothing is written back to disk
    and no local path is returned to the caller.
    """
    row = connection.execute(
        "SELECT storage_name, extension, original_name, display_name, mime_type FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    target = _safe_storage_path(row["storage_name"])
    if not target.exists():
        raise HTTPException(status_code=404, detail="The stored document file is missing.")
    try:
        content = _open(target.read_bytes())
    except crypto.EncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    name = row["display_name"] or row["original_name"] or f"document{row['extension']}"
    if not name.lower().endswith(row["extension"]):
        name = f"{name}{row['extension']}"
    media_type = row["mime_type"] or MIME_TYPES.get(row["extension"], "application/octet-stream")
    return content, media_type, name


def get_document_pages(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    page: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    document = get_document(connection, document_id)
    if page is not None:
        rows = connection.execute(
            """SELECT page_number, content, status, extraction_method, ocr_confidence, error_category, chunk_count
               FROM document_pages
               WHERE document_id = ? AND COALESCE(page_number, 0) = ? ORDER BY rowid""",
            (document_id, page),
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT page_number, content, status, extraction_method, ocr_confidence, error_category, chunk_count
               FROM document_pages
               WHERE document_id = ? ORDER BY page_number, rowid LIMIT ?""",
            (document_id, max(1, min(limit, 500))),
        ).fetchall()
    pages = [
        {
            "page_number": row["page_number"],
            "content": row["content"],
            "status": row["status"],
            "method": row["extraction_method"],
            "ocr_confidence": row["ocr_confidence"],
            "error_category": row["error_category"],
            "chunk_count": row["chunk_count"],
        }
        for row in rows
    ]
    return {"document": document, "pages": pages, "requested_page": page, "page_count": len(pages)}


def update_document_metadata(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    display_name: str,
    description: str = "",
    category: str = "Other",
) -> dict[str, Any]:
    existing = connection.execute("SELECT original_name FROM documents WHERE id = ?", (document_id,)).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    title = _clean_display_name(display_name, existing["original_name"])
    description_text = _clean_description(description)
    category_value = normalize_category(category)
    connection.execute(
        "UPDATE documents SET display_name = ?, description = ?, category = ? WHERE id = ?",
        (title, description_text, category_value, document_id),
    )
    connection.commit()
    return get_document(connection, document_id)


def bulk_delete_documents(connection: sqlite3.Connection, document_ids: list[str]) -> dict[str, Any]:
    """Deletes exactly the given document ids and reports each outcome."""
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    seen: set[str] = set()
    for document_id in document_ids:
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        try:
            delete_document(connection, document_id)
            deleted.append(document_id)
        except HTTPException as exc:
            failed.append({"document_id": document_id, "error": str(exc.detail)})
        except (OSError, sqlite3.Error):
            failed.append({"document_id": document_id, "error": "The document could not be deleted."})
    return {"deleted": deleted, "failed": failed}


def delete_document(connection: sqlite3.Connection, document_id: str) -> None:
    row = connection.execute(
        "SELECT storage_name FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    target = _safe_storage_path(row["storage_name"])
    try:
        target.unlink(missing_ok=True)
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        connection.rollback()
        raise HTTPException(status_code=500, detail="The document could not be completely deleted.") from exc


def backfill_document_types(connection: sqlite3.Connection) -> int:
    """Classifies documents indexed before document_type existed."""
    rows = connection.execute(
        "SELECT id, COALESCE(display_name, original_name) AS name FROM documents "
        "WHERE document_type IS NULL OR TRIM(document_type) = ''"
    ).fetchall()
    updated = 0
    for row in rows:
        pages = connection.execute(
            "SELECT page_number, content, ocr_confidence FROM document_pages "
            "WHERE document_id = ? AND status = 'ok' AND TRIM(content) <> ''",
            (row["id"],),
        ).fetchall()
        if not pages:
            continue
        typed = sorted(
            document_fields.document_types(
                row["name"] or "",
                [
                    document_fields.Page(
                        document_id=row["id"],
                        document_name=row["name"] or "",
                        page_number=page["page_number"] or 1,
                        text=page["content"] or "",
                        confidence=page["ocr_confidence"],
                    )
                    for page in pages
                ],
            )
        )
        connection.execute(
            "UPDATE documents SET document_type = ? WHERE id = ?",
            (",".join(typed) or None, row["id"]),
        )
        updated += 1
    connection.commit()
    return updated


def recover_interrupted_jobs(connection: sqlite3.Connection) -> int:
    """Returns interrupted processing to a recoverable state after a restart."""
    changed = connection.execute(
        "UPDATE document_processing_jobs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
        (utc_now(),),
    ).rowcount
    documents = connection.execute(
        "SELECT id FROM documents WHERE status = 'processing' OR processing_stage IS NOT NULL"
    ).fetchall()
    for row in documents:
        connection.execute(
            """UPDATE documents SET status = 'retry_required', processing_stage = NULL,
               processing_error = ? WHERE id = ? AND (status = 'processing' OR processing_stage IS NOT NULL)""",
            (
                "Processing was interrupted when the backend stopped. Retry processing to finish it.",
                row["id"],
            ),
        )
    connection.commit()
    return int(changed or 0)


def document_processing_health(connection: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    return document_pipeline.pipeline_health(connection, force=force)


def document_consistency(connection: sqlite3.Connection) -> dict[str, Any]:
    return document_pipeline.consistency_report(connection)


def _expanded_query_terms(query: str) -> list[str]:
    query = query_understanding.canonicalize_query(query)
    terms = [
        term
        for term in re.findall(r"[\w'-]+", query.casefold())
        if len(term) > 1 and term not in SEARCH_STOP_WORDS
    ]
    if not terms:
        return []

    expanded = set(terms)
    for term in list(terms):
        expanded.update(QUERY_SYNONYMS.get(term, set()))

    if any(term in {"about", "profile", "background", "self", "myself"} for term in terms):
        expanded.update({"profile", "background", "overview", "experience", "developer", "study", "studying", "education", "location", "based"})
    if any(term in {"where", "based", "location", "study", "studying", "from"} for term in terms):
        expanded.update({"location", "based", "home", "study", "studying", "education"})
    if any(term in {"skill", "skills", "technology", "technologies", "learn", "learning", "career", "goal", "direction"} for term in terms):
        expanded.update({"skill", "skills", "technology", "technologies", "learning", "career", "goal", "direction"})
    if any(term in {"project", "matrix", "architecture", "system", "application"} for term in terms):
        expanded.update({"project", "matrix", "architecture", "system", "application", "technology", "limitations"})
    if any(term in {"limit", "limits", "limitations", "issue", "issues", "concern", "concerns", "risk", "risks"} for term in terms):
        expanded.update({"limit", "limitations", "issues", "concerns", "risks", "tradeoff", "tradeoffs"})

    return sorted(expanded, key=lambda item: (-len(item), item))[:14]


_BM25_K1 = 1.2
_BM25_B = 0.75
_RELEVANCE_RATIO = 0.4
_PROXIMITY_WINDOW = 180
_MAX_RESULTS_PER_DOCUMENT = 2


def _corpus_statistics(
    connection: sqlite3.Connection,
    terms: list[str],
) -> tuple[int, float, dict[str, int]]:
    """Returns chunk count, average chunk length and per-term chunk frequency."""
    total, average_length = connection.execute(
        "SELECT COUNT(*), COALESCE(AVG(LENGTH(content)), 1) FROM document_chunks"
    ).fetchone()
    frequencies: dict[str, int] = {}
    for term in terms:
        pattern = f"%{_escape_like(term)}%"
        frequencies[term] = connection.execute(
            """SELECT COUNT(*) FROM document_chunks
               WHERE lower(content) LIKE ? ESCAPE '\\'
                  OR lower(COALESCE(section_heading, '')) LIKE ? ESCAPE '\\'""",
            (pattern, pattern),
        ).fetchone()[0]
    return int(total or 0), max(float(average_length or 1), 1.0), frequencies


def _bm25_idf(total_chunks: int, frequency: int) -> float:
    if total_chunks <= 0:
        return 1.0
    bounded = min(max(frequency, 0), total_chunks)
    return math.log(1 + (total_chunks - bounded + 0.5) / (bounded + 0.5))


def _query_concepts(query: str) -> list[set[str]]:
    """Groups each meaningful query word with its synonyms as one concept."""
    concepts: list[set[str]] = []
    for term in re.findall(r"[\w'-]+", query.casefold()):
        if len(term) <= 1 or term in SEARCH_STOP_WORDS:
            continue
        concepts.append({term, *QUERY_SYNONYMS.get(term, set())})
    return concepts


def _rank_chunk(
    content: str,
    section_heading: str | None,
    terms: list[str],
    *,
    total_chunks: int = 0,
    average_length: float = 1.0,
    frequencies: dict[str, int] | None = None,
) -> float:
    """Scores one chunk with BM25 weighting plus heading and proximity boosts."""
    folded = content.casefold()
    heading = (section_heading or "").casefold()
    frequencies = frequencies or {}
    length = max(len(content), 1)
    matched = 0
    score = 0.0
    for term in terms:
        if not term:
            continue
        count = folded.count(term)
        heading_hit = term in heading
        if not count and not heading_hit:
            continue
        matched += 1
        term_frequency = count + (2 if heading_hit else 0)
        idf = _bm25_idf(total_chunks, frequencies.get(term, 0))
        denominator = term_frequency + _BM25_K1 * (
            1 - _BM25_B + _BM25_B * length / average_length
        )
        score += idf * (term_frequency * (_BM25_K1 + 1)) / denominator
    if not matched:
        return 0.0
    score *= 1 + 0.12 * (matched - 1)
    if matched > 1:
        positions = [folded.find(term) for term in terms if term and folded.find(term) >= 0]
        if positions and max(positions) - min(positions) <= _PROXIMITY_WINDOW:
            score *= 1.15
    return score


def _document_metadata_score(metadata: str, terms: list[str]) -> float:
    """Metadata relevance: an explicit document name should outrank incidental text."""
    folded = metadata.casefold()
    score = 0.0
    for term in terms:
        if term and term in folded:
            score += 1.25 if len(term) >= 5 else 0.9
    return score


def _field_label_score(query: str, content: str) -> float:
    folded_query = query.casefold()
    folded_content = content.casefold()
    boost = 0.0
    if re.search(r"\bpan\b|permanent\s+account\s+number", folded_query):
        if re.search(r"\bpan\b|permanent\s+account\s+number", folded_content):
            boost += 3.0
    if re.search(r"aadha?ar|adhar|aashar|\buid\b", folded_query):
        if re.search(r"aadha?ar|adhar|aashar|uidai|\buid\b", folded_content):
            boost += 3.0
    if re.search(r"\b(?:dob|date\s+of\s+birth|birth\s+date|born)\b", folded_query):
        if re.search(r"date\s+of\s+birth|d\.?o\.?b\.?|\bdob\b|birth\s*date", folded_content):
            boost += 2.5
    if re.search(r"\b(?:marks|marksheet|score|percentage|grade)\b", folded_query):
        if re.search(r"\b(?:marks|total|score|percentage|grade|result)\b", folded_content):
            boost += 1.5
    return boost


def _page_context_excerpt(
    connection: sqlite3.Connection,
    document_id: str,
    page_number: int | None,
    terms: list[str],
) -> str | None:
    """Returns every chunk on a matched page so labels and values stay together."""
    if page_number is None:
        return None
    rows = connection.execute(
        "SELECT content FROM document_chunks WHERE document_id = ? AND page_number IS ? "
        "ORDER BY chunk_index",
        (document_id, page_number),
    ).fetchall()
    if not rows:
        return None
    return _excerpt("\n".join(row["content"] or "" for row in rows), terms, width=2200)


def search_documents(connection: sqlite3.Connection, request: DocumentSearchRequest) -> list[dict[str, Any]]:
    terms = _expanded_query_terms(request.query)
    if not terms:
        return []
    document_ids = [value for value in (request.document_ids or []) if value]
    document_filter = ""
    filter_parameters: list[str] = []
    if document_ids:
        placeholders = ", ".join("?" for _ in document_ids)
        document_filter = f" AND d.id IN ({placeholders})"
        filter_parameters = document_ids
    chunk_where = " OR ".join("lower(c.content) LIKE ? ESCAPE '\\'" for _ in terms)
    heading_where = " OR ".join("lower(c.section_heading) LIKE ? ESCAPE '\\'" for _ in terms)
    title_where = " OR ".join("lower(COALESCE(d.display_name, d.original_name)) LIKE ? ESCAPE '\\'" for _ in terms)
    description_where = " OR ".join("lower(d.description) LIKE ? ESCAPE '\\'" for _ in terms)
    parameters = [f"%{_escape_like(term)}%" for term in terms]
    # Only documents with a usable index answer questions. Failed/retry documents
    # are surfaced separately by documents_requiring_attention().
    chunk_rows = connection.execute(
        f"""SELECT d.id AS document_id, d.original_name, d.display_name, d.description, d.category,
                  d.document_type, d.status AS document_status, c.page_number, c.section_heading, c.content,
                  (SELECT MAX(p.ocr_confidence) FROM document_pages p
                   WHERE p.document_id = d.id AND p.page_number = c.page_number) AS ocr_confidence
            FROM document_chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.status IN ('indexed', 'partial')
              AND ({chunk_where} OR {heading_where} OR {title_where} OR {description_where}){document_filter}
            ORDER BY c.document_id, c.chunk_index LIMIT 1000""",
        [*parameters, *parameters, *parameters, *parameters, *filter_parameters],
    ).fetchall()
    total_chunks, average_length, frequencies = _corpus_statistics(connection, terms)
    concepts = _query_concepts(request.query)
    required_coverage = (len(concepts) + 1) // 2 if concepts else 0
    requested_types = set(query_understanding.document_types_for_query(request.query))
    type_keywords = _document_type_keywords()
    ranked = []
    seen = set()
    content_seen: set[str] = set()
    for row in chunk_rows:
        content = row["content"] or ""
        metadata = " ".join(
            filter(None, [row["display_name"], row["original_name"], row["description"], row["category"]])
        )
        document_types = {
            item for item in (row["document_type"] or "").split(",") if item
        }
        if not document_types:
            folded_metadata = metadata.casefold()
            document_types = {
                doc_type
                for doc_type, keywords in type_keywords.items()
                if any(term in folded_metadata for term in keywords)
            }
        document_title = f"{row['display_name'] or ''} {row['original_name'] or ''}".casefold()
        document_description = (row["description"] or "").casefold()
        title_matched = any(term in document_title for term in terms)
        description_matched = any(term in document_description for term in terms)
        if required_coverage and not title_matched:
            folded = content.casefold()
            heading = (row["section_heading"] or "").casefold()
            covered = sum(
                1
                for variants in concepts
                if any(variant in folded or variant in heading for variant in variants)
            )
            if covered < required_coverage:
                continue
        score = _rank_chunk(
            content,
            row["section_heading"],
            terms,
            total_chunks=total_chunks,
            average_length=average_length,
            frequencies=frequencies,
        )
        if title_matched:
            score = (score or 0.0) * 1.6 + 2.2
        elif description_matched:
            score = (score or 0.0) * 1.2 + 0.9
        score += _document_metadata_score(metadata, terms) + _field_label_score(request.query, content)
        if not score:
            continue
        if requested_types and document_types:
            if document_types & requested_types:
                score *= 1.35
            else:
                # Demote rather than exclude: a genuine answer may still live in a
                # document whose type could not be classified.
                score *= 0.55
        normalized_content = " ".join(content.split()).casefold()
        if len(normalized_content) > 80:
            fingerprint = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
            if fingerprint in content_seen:
                continue
            content_seen.add(fingerprint)
        key = (
            row["document_id"],
            row["page_number"],
            row["section_heading"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        if key in seen:
            continue
        seen.add(key)
        ranked.append((score, row))
    ranked.sort(key=lambda item: (-item[0], item[1]["document_id"], item[1]["page_number"] or 0))
    if not ranked:
        return []
    wide = bool(getattr(request, "wide", False))
    relevance_floor = ranked[0][0] * (0.25 if wide else _RELEVANCE_RATIO)
    max_per_document = 4 if wide else _MAX_RESULTS_PER_DOCUMENT
    target_limit = max(request.limit, 6) if wide else request.limit
    results = []
    per_document: dict[str, int] = {}
    for score, row in ranked:
        if len(results) >= target_limit:
            break
        if score < relevance_floor:
            break
        count = per_document.get(row["document_id"], 0)
        if count >= max_per_document:
            continue
        per_document[row["document_id"]] = count + 1
        content = row["content"] or ""
        document_types = {
            item for item in (row["document_type"] or "").split(",") if item
        }
        if not document_types:
            folded_metadata = " ".join(
                filter(None, [row["display_name"], row["original_name"], row["description"], row["category"]])
            ).casefold()
            document_types = {
                doc_type
                for doc_type, keywords in type_keywords.items()
                if any(term in folded_metadata for term in keywords)
            }
        results.append(
            {
                "document_id": row["document_id"],
                "document_name": row["display_name"] or row["original_name"],
                "document_status": row["document_status"],
                "category": row["category"] or "Other",
                "document_type": ",".join(sorted(document_types)) or None,
                "filename": row["display_name"] or row["original_name"],
                "original_filename": row["original_name"],
                "page_number": row["page_number"],
                "section_heading": row["section_heading"],
                "snippet": _snippet(content, terms),
                # Page context expansion: neighbouring chunks from the same OCR
                # page are included so a label and its value are never separated.
                "excerpt": _page_context_excerpt(
                    connection, row["document_id"], row["page_number"], terms
                )
                or _excerpt(content, terms),
                "relevance_score": round(score / max(len(set(terms)), 1), 4),
                "ocr_confidence": row["ocr_confidence"],
            }
        )
    return results


def documents_requiring_attention(
    connection: sqlite3.Connection,
    query: str,
) -> list[dict[str, Any]]:
    """Finds unreadable/partial documents that match a query's metadata.

    Used so AI Chat can say "this document has not been read yet" instead of
    claiming the information does not exist.
    """
    terms = _expanded_query_terms(query)
    if not terms:
        return []
    rows = connection.execute(
        """SELECT id, display_name, original_name, description, category, status, page_count,
                  processed_pages, failed_pages, processing_error, ocr_status
           FROM documents WHERE status IN ('failed', 'retry_required', 'partial', 'processing')
           ORDER BY created_at DESC LIMIT 50"""
    ).fetchall()
    matches = []
    for row in rows:
        metadata = " ".join(
            filter(None, [row["display_name"], row["original_name"], row["description"], row["category"]])
        ).casefold()
        hits = sum(1 for term in terms if term and term in metadata)
        if not hits:
            continue
        matches.append(
            {
                "document_id": row["id"],
                "filename": row["display_name"] or row["original_name"],
                "document_name": row["display_name"] or row["original_name"],
                "status": row["status"],
                "page_count": row["page_count"],
                "processed_pages": row["processed_pages"] or 0,
                "failed_pages": row["failed_pages"] or 0,
                "processing_error": row["processing_error"],
                "ocr_status": row["ocr_status"],
                "match_score": hits,
            }
        )
    matches.sort(key=lambda item: -item["match_score"])
    return matches[:3]


def _sources_with_full_pages(
    connection: sqlite3.Connection | None,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expands each source excerpt with its full stored page for field extraction."""
    if connection is None:
        return sources
    enriched: list[dict[str, Any]] = []
    for source in sources:
        item = dict(source)
        document_id = source.get("document_id")
        if document_id:
            row = connection.execute(
                """SELECT content FROM document_pages
                   WHERE document_id = ? AND page_number IS ? AND status = 'ok'
                     AND TRIM(content) <> '' ORDER BY rowid LIMIT 1""",
                (document_id, source.get("page_number")),
            ).fetchone()
            if row is None and source.get("page_number") is not None:
                row = connection.execute(
                    """SELECT content FROM document_pages
                       WHERE document_id = ? AND status = 'ok'
                         AND TRIM(content) <> '' ORDER BY page_number LIMIT 1""",
                    (document_id,),
                ).fetchone()
            if row is not None and row["content"]:
                item["excerpt"] = row["content"]
        enriched.append(item)
    return enriched


def _document_type_keywords() -> dict[str, tuple[str, ...]]:
    return {
        "pan": ("pan", "permanent account"),
        "aadhaar": ("aadhaar", "aadhar", "adhar", "aashar", "uidai", "uid"),
        "marksheet": ("marksheet", "mark sheet", "marks statement", "12th", "class 12", "result"),
        "migration": ("migration", "transfer certificate"),
    }


TYPE_CONTENT_TERMS: dict[str, tuple[str, ...]] = {
    "pan": ("permanent account", "income tax", " pan "),
    "aadhaar": ("aadhaar", "aadhar", "uidai", "unique identification"),
    "marksheet": ("marks obtained", "board of secondary", "class xii", "class 12", "mathematics"),
    "migration": ("migration certificate", "transfer certificate"),
    "driving_licence": ("driving licence", "driving license", "transport department"),
}


def _requested_document_types(query: str) -> set[str]:
    return set(query_understanding.document_types_for_query(query))


def answer_document_question(
    connection: sqlite3.Connection,
    query: str,
    sources: list[dict[str, Any]],
    *,
    widen: bool = False,
) -> dict[str, Any] | None:
    """Builds a structured, page-grounded answer from complete stored pages."""
    requested = _requested_document_types(query)
    rows = connection.execute(
        """SELECT d.id, d.display_name, d.original_name, d.description, d.category,
                  d.document_type, d.status, d.page_count, d.processed_pages, d.failed_pages
           FROM documents d
           WHERE d.status IN ('indexed', 'partial', 'processing', 'retry_required', 'failed')
           ORDER BY d.created_at DESC LIMIT 60"""
    ).fetchall()
    source_document_ids = {source.get("document_id") for source in sources if source.get("document_id")}
    keywords = _document_type_keywords()
    selected: list[tuple[Any, set[str]]] = []
    for row in rows:
        metadata = " ".join(
            filter(None, [row["display_name"], row["original_name"], row["description"], row["category"]])
        ).casefold()
        matched = {item for item in (row["document_type"] or "").split(",") if item}
        if not matched:
            matched = {
                doc_type for doc_type, terms in keywords.items() if any(term in metadata for term in terms)
            }
        if requested:
            if not (matched & requested):
                continue
        elif row["id"] not in source_document_ids:
            continue
        selected.append((row, matched or requested))
        if len(selected) >= 8:
            break
    if widen and requested and len(selected) < 8:
        # A repeated, unresolved question searches page content directly instead
        # of relying on document names or stored classification alone.
        selected_ids = {row["id"] for row, _ in selected}
        for document_type in sorted(requested):
            content_terms = TYPE_CONTENT_TERMS.get(document_type, ())
            if not content_terms:
                continue
            clause = " OR ".join("lower(content) LIKE ? ESCAPE '\\'" for _ in content_terms)
            matches = connection.execute(
                f"SELECT DISTINCT document_id FROM document_pages WHERE status = 'ok' AND ({clause})",
                [f"%{term.strip()}%" for term in content_terms],
            ).fetchall()
            for match in matches:
                if match["document_id"] in selected_ids:
                    continue
                candidate = next((row for row in rows if row["id"] == match["document_id"]), None)
                if candidate is None:
                    continue
                selected.append((candidate, {document_type}))
                selected_ids.add(candidate["id"])
                if len(selected) >= 8:
                    break
            if len(selected) >= 8:
                break
    if not selected and source_document_ids:
        for row in rows:
            if row["id"] in source_document_ids:
                selected.append((row, requested))
                if len(selected) >= 4:
                    break

    pages: list[document_fields.Page] = []
    incomplete: list[dict[str, Any]] = []
    for row, _ in selected:
        page_rows = connection.execute(
            """SELECT page_number, content, ocr_confidence, status
               FROM document_pages WHERE document_id = ?
               ORDER BY page_number LIMIT 30""",
            (row["id"],),
        ).fetchall()
        readable = [
            document_fields.Page(
                document_id=row["id"],
                document_name=row["display_name"] or row["original_name"],
                page_number=page["page_number"],
                text=page["content"] or "",
                confidence=page["ocr_confidence"],
                status=page["status"],
            )
            for page in page_rows
            if page["content"] and page["status"] == "ok"
        ]
        pages.extend(readable)
        if row["status"] in {"partial", "processing", "retry_required", "failed"} or (row["failed_pages"] or 0) > 0:
            incomplete.append(
                {
                    "document_id": row["id"],
                    "document_name": row["display_name"] or row["original_name"],
                    "status": row["status"],
                    "failed_pages": row["failed_pages"] or 0,
                }
            )

    result = document_fields.answer_document_question(
        query, pages, incomplete_documents=incomplete
    )
    if result is None:
        return None

    page_lookup = {(page.document_id, page.page_number): page for page in pages}
    answer_state = result["state"]
    if answer_state == "not_found" and incomplete and requested:
        answer_state = "incomplete"
        result["answer"] = (
            "Some of the matching pages haven't been processed completely, so I can't confirm that "
            "information yet. Check the Documents page and retry processing if needed."
        )
    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for candidate in result.get("citations") or []:
        key = (candidate.document_id, candidate.page)
        if key in seen:
            continue
        seen.add(key)
        page = page_lookup.get(key)
        text = page.text if page else ""
        needle = candidate.value or (candidate.basis or "")
        position = text.casefold().find(needle.casefold()) if needle else -1
        start = max(0, position - 80) if position >= 0 else 0
        snippet = " ".join(text[start : start + 280].split())
        citations.append(
            {
                "kind": "document",
                "document_id": candidate.document_id,
                "filename": candidate.document_name,
                "document_name": candidate.document_name,
                "original_filename": candidate.document_name,
                "category": None,
                "page_number": candidate.page,
                "section_heading": None,
                "snippet": snippet,
                "excerpt": snippet,
                "relevance_score": 1.0,
                "ocr_confidence": candidate.confidence,
                "document_status": "indexed",
                "answer_state": answer_state,
                "requires_verification": answer_state != "confirmed",
            }
        )
    result["state"] = answer_state
    result["sources"] = citations
    result.pop("citations", None)
    result.pop("candidates", None)
    return result


def sources_support_query(query: str, sources: list[dict[str, Any]]) -> bool:
    """Checks whether any retrieved page actually mentions a query concept."""
    generic = {
        "number", "card", "document", "documents", "information", "details",
        "available", "written", "mentioned", "tell", "show", "page", "value",
    }
    terms = [term for term in _expanded_query_terms(query) if term not in generic]
    if not terms:
        return True
    haystack = " ".join(
        f"{source.get('excerpt') or ''} {source.get('snippet') or ''}".casefold()
        for source in sources
    )
    return any(term in haystack for term in terms)


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keeps one citation per document/page and preserves answer metadata."""
    merged: dict[tuple[str, Any], dict[str, Any]] = {}
    for source in sources:
        key = (str(source.get("document_id") or source.get("path") or ""), source.get("page_number"))
        if key not in merged:
            merged[key] = source
    return list(merged.values())


def extract_exact_field(
    query: str,
    sources: list[dict[str, Any]],
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Deterministic PAN/Aadhaar/DOB evidence used to ground short answers."""
    return document_pipeline.extract_exact_field(
        query, _sources_with_full_pages(connection, sources)
    )


def field_label_evidence(
    query: str,
    sources: list[dict[str, Any]],
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Finds a requested field label when no trustworthy value could be read."""
    return document_pipeline.field_label_evidence(
        query, _sources_with_full_pages(connection, sources)
    )


def validate_sources(message: str, sources: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    """Drops citations the answer does not actually use.

    A source is only returned when the answer cites it, or when the answer is a
    direct factual statement and the source contains a query concept. An
    unsupported ("I could not find ...") answer must return no sources.
    """
    if not sources:
        return []
    unsupported = bool(
        re.search(
            r"(?:could(?:n't| not) find|not (?:present|available|found|included|mentioned)|"
            r"no (?:information|evidence|mention)|unable to find|does not (?:contain|include|mention))",
            (answer or "").casefold(),
        )
    )
    if unsupported:
        return []
    cited = {
        int(value)
        for value in re.findall(r"\[Source\s+(\d+)\]", answer or "", flags=re.IGNORECASE)
    }
    if cited:
        kept = [source for index, source in enumerate(sources, start=1) if index in cited]
        if kept:
            return kept
    terms = _expanded_query_terms(message) or [term for term in re.findall(r"[\w'-]+", message.casefold()) if len(term) > 2]
    validated = []
    for source in sources:
        text = f"{source.get('excerpt') or ''} {source.get('snippet') or ''}".casefold()
        if not terms or any(term in text for term in terms):
            validated.append(source)
    return dedupe_sources(validated)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text: str, terms: list[str], width: int = 260) -> str:
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    start = max(0, min(positions, default=0) - 70)
    snippet = " ".join(text[start : start + width].split())
    return ("…" if start else "") + snippet + ("…" if start + width < len(text) else "")


def _excerpt(text: str, terms: list[str], width: int = 700) -> str:
    """Returns a wider passage used as evidence and for citation inspection."""
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    start = max(0, min(positions, default=0) - 120)
    excerpt = " ".join(text[start : start + width].split())
    return ("…" if start else "") + excerpt + ("…" if start + width < len(text) else "")
