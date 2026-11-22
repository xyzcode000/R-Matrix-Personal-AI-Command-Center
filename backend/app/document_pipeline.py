"""Local OCR resolution, page-level document extraction and processing health.

This module is the single source of truth for "can this machine read a document
right now?".  It validates the actual Tesseract executable (not merely the
``pytesseract`` wrapper), lists installed languages, runs a real OCR probe on a
synthetic image, and reports the result through :func:`pipeline_health`.

Nothing here prints or stores document text.  Extracted text only travels back
to the caller (:mod:`app.documents`), which persists it through the existing
encrypted database/storage flow.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import settings

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MULTIPAGE_IMAGE_EXTENSIONS = {".tif", ".tiff"}
EXPECTED_IMAGE_FORMATS = {
    ".png": {"PNG"},
    ".jpg": {"JPEG"},
    ".jpeg": {"JPEG"},
    ".webp": {"WEBP"},
    ".bmp": {"BMP"},
    ".tif": {"TIFF"},
    ".tiff": {"TIFF"},
}
SUPPORTED_FORMAT_MESSAGE = (
    "Supported formats are PDF, DOC/DOCX, TXT, Markdown, CSV, XLSX, PPTX, PNG, JPG/JPEG, WEBP, BMP and TIFF."
)
WINDOWS_CREATE_NO_WINDOW = 0x08000000
_OCR_LOCK = threading.Lock()
_RESOLVE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 30

_ACTIVE_DOCUMENTS: dict[str, dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()

# Language labels are reported to the owner; the traineddata file names stay internal.
LANGUAGE_LABELS = {"eng": "English", "hin": "Hindi", "osd": "Orientation & script detection"}


class DocumentProcessingError(RuntimeError):
    """A sanitized, categorised processing failure with optional page results."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "processing_error",
        retryable: bool = False,
        pages: list["PageResult"] | None = None,
        detected_pages: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.retryable = retryable
        self.pages = pages
        self.detected_pages = detected_pages


# ---------------------------------------------------------------------------
# OCR engine discovery and verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrEngine:
    command: str
    version: str
    languages: tuple[str, ...]
    source: str
    tessdata_prefix: str | None
    requested_languages: tuple[str, ...]
    usable_languages: tuple[str, ...]
    missing_languages: tuple[str, ...]
    probe_passed: bool
    probe_detail: str

    @property
    def language_argument(self) -> str:
        return "+".join(self.usable_languages)


@dataclass
class _Resolution:
    engine: OcrEngine | None
    errors: list[dict[str, str]] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


_RESOLVED: tuple[float, _Resolution] | None = None


def _creation_flags() -> int:
    return WINDOWS_CREATE_NO_WINDOW if os.name == "nt" else 0


def _run(command: list[str], *, env: dict[str, str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
        creationflags=_creation_flags(),
    )


def _decode(value: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return value.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return value.decode("utf-8", errors="replace")


def _windows_candidates() -> list[str]:
    roots: list[Path] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA", "APPDATA"):
        value = os.getenv(variable)
        if value:
            roots.append(Path(value))
    candidates = []
    for root in roots:
        candidates.extend(
            [
                root / "Tesseract-OCR" / "tesseract.exe",
                root / "Tesseract OCR" / "tesseract.exe",
                root / "Programs" / "Tesseract-OCR" / "tesseract.exe",
            ]
        )
    return [str(path) for path in candidates]


def _ocr_candidates() -> list[tuple[str, str]]:
    """Returns (command, source) pairs in resolution order."""
    candidates: list[tuple[str, str]] = []
    configured = settings.tesseract_cmd
    if configured:
        candidates.append((configured, "TESSERACT_CMD"))
    found = shutil.which("tesseract")
    if found:
        candidates.append((found, "PATH"))
    try:
        import pytesseract

        package_command = getattr(pytesseract.pytesseract, "tesseract_cmd", "")
        if package_command and package_command not in {"tesseract", ""}:
            candidates.append((package_command, "pytesseract configuration"))
    except ImportError:
        pass
    if os.name == "nt":
        candidates.extend((path, "standard Windows install location") for path in _windows_candidates())
    else:
        candidates.extend(
            (path, "standard install location")
            for path in ("/usr/bin/tesseract", "/usr/local/bin/tesseract", "/opt/homebrew/bin/tesseract")
        )
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for command, source in candidates:
        key = str(command).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append((command, source))
    return unique


def _tessdata_for(command: str) -> str | None:
    """Resolves the tessdata directory without overriding a valid explicit setting."""
    configured = settings.tessdata_prefix
    if configured:
        path = Path(configured)
        # Tesseract accepts either the tessdata folder or its parent.
        if (path / "eng.traineddata").exists() or (path / "tessdata" / "eng.traineddata").exists():
            return str(path if path.name.casefold() == "tessdata" else path / "tessdata" if (path / "tessdata").is_dir() else path)
        return None
    candidate = Path(command).resolve().parent / "tessdata"
    if (candidate / "eng.traineddata").exists():
        return str(candidate)
    return None


def _probe_engine(command: str, source: str) -> tuple[OcrEngine | None, dict[str, str] | None]:
    entry = Path(command)
    if not entry.exists() and not shutil.which(str(command)):
        return None, {
            "category": "ocr_invalid_path",
            "message": f"The configured OCR executable was not found ({source}).",
        }
    tessdata = _tessdata_for(str(command))
    environment = os.environ.copy()
    if settings.tessdata_prefix and tessdata is None:
        return None, {
            "category": "ocr_invalid_tessdata",
            "message": "TESSDATA_PREFIX is set but does not contain English trained data (eng.traineddata).",
        }
    if tessdata:
        environment["TESSDATA_PREFIX"] = tessdata
    try:
        completed = _run([str(command), "--version"], env=environment)
    except (OSError, subprocess.SubprocessError):
        return None, {
            "category": "ocr_invalid_path",
            "message": f"The OCR executable could not be started ({source}).",
        }
    if completed.returncode != 0:
        return None, {
            "category": "ocr_invalid_path",
            "message": f"The OCR executable did not report a valid version ({source}).",
        }
    version_text = _decode(completed.stdout or completed.stderr)
    version_line = next((line.strip() for line in version_text.splitlines() if line.strip()), "")
    version = re.sub(r"^tesseract\s+", "", version_line, flags=re.I).lstrip("vV")[:80]
    try:
        languages_run = _run([str(command), "--list-langs"], env=environment)
    except (OSError, subprocess.SubprocessError):
        languages_run = None
    languages: list[str] = []
    language_error: str | None = None
    if languages_run is not None:
        if languages_run.returncode == 0:
            for line in _decode(languages_run.stdout).splitlines():
                value = line.strip()
                if not value or "available languages" in value.casefold() or value.endswith(":"):
                    continue
                if " " in value:
                    continue
                languages.append(value.casefold())
        else:
            language_error = "The installed OCR engine could not list its language data."
    if "eng" not in languages:
        return None, {
            "category": "ocr_missing_language",
            "message": language_error
            or "English OCR language data (eng.traineddata) is missing from the OCR installation.",
        }
    requested = tuple(settings.requested_ocr_languages)
    usable = tuple(language for language in requested if language in languages) or ("eng",)
    missing = tuple(language for language in requested if language not in languages)
    probe_passed, probe_detail = _functional_probe(str(command), usable, environment)
    engine = OcrEngine(
        command=str(command),
        version=version or "unknown",
        languages=tuple(sorted(languages)),
        source=source,
        tessdata_prefix=tessdata,
        requested_languages=requested,
        usable_languages=usable,
        missing_languages=missing,
        probe_passed=probe_passed,
        probe_detail=probe_detail,
    )
    if not probe_passed:
        return engine, {
            "category": "ocr_probe_failed",
            "message": probe_detail,
        }
    return engine, None


def _functional_probe(command: str, languages: Iterable[str], environment: dict[str, str]) -> tuple[bool, str]:
    """Runs the engine on a generated image to prove it can actually OCR."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False, "Pillow is not installed, so the OCR engine could not be exercised."
    image = Image.new("RGB", (640, 160), "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in (
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 44)
            break
        except (OSError, ValueError):
            continue
    draw.text((24, 54), "OCR READY 12345", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    language = "+".join(languages)
    try:
        with _OCR_LOCK:
            completed = subprocess.run(
                [command, "stdin", "stdout", "-l", language, "--psm", "6"],
                input=buffer.getvalue(),
                capture_output=True,
                check=False,
                timeout=25,
                env=environment,
                creationflags=_creation_flags(),
            )
    except (OSError, subprocess.SubprocessError):
        return False, "The OCR engine could not run its verification image."
    text = _decode(completed.stdout if completed.returncode == 0 else b"")
    if completed.returncode != 0:
        return False, "The OCR engine failed on a synthetic verification image."
    letters = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if len(letters) < 5:
        return False, "The OCR engine returned no readable text for its verification image."
    return True, "The OCR engine read its synthetic verification image."


def resolve_ocr(*, force: bool = False) -> _Resolution:
    """Resolves and verifies the OCR engine. Results are cached briefly."""
    global _RESOLVED
    with _RESOLVE_LOCK:
        now = time.monotonic()
        if not force and _RESOLVED and now - _RESOLVED[0] < _CACHE_TTL_SECONDS:
            return _RESOLVED[1]
        errors: list[dict[str, str]] = []
        candidates = _ocr_candidates()
        resolution = _Resolution(None, errors, [command for command, _ in candidates])
        for command, source in candidates:
            engine, error = _probe_engine(command, source)
            if engine is not None and engine.probe_passed:
                resolution.engine = engine
                break
            if engine is not None:
                resolution.engine = engine
            if error:
                errors.append({**error, "source": source})
        if resolution.engine is not None and not resolution.engine.probe_passed:
            # Keep the engine details for diagnostics, but it is not usable.
            resolution.engine = resolution.engine
        _RESOLVED = (now, resolution)
        return resolution


def reset_ocr_cache() -> None:
    """Forces the next health check/resolution to re-read the environment."""
    global _RESOLVED
    with _RESOLVE_LOCK:
        _RESOLVED = None


def ocr_engine(*, force: bool = False) -> OcrEngine | None:
    engine = resolve_ocr(force=force).engine
    return engine if engine is not None and engine.probe_passed else None


def ocr_error_message() -> str:
    engine = resolve_ocr().engine
    if engine is not None and not engine.probe_passed:
        return engine.probe_detail
    errors = resolve_ocr().errors
    if errors:
        return errors[0]["message"]
    return "The local OCR engine (Tesseract) is not available."


# ---------------------------------------------------------------------------
# OCR execution
# ---------------------------------------------------------------------------


def prepare_image(image: Any) -> Any:
    """Conservative, local preprocessing that never alters the stored original."""
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    if image.mode not in {"RGB", "L", "RGBA"}:
        image = image.convert("RGB")
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.split()[-1])
        image = background
    width, height = image.size
    if width <= 0 or height <= 0:
        raise DocumentProcessingError("The image has no usable pixels.", category="image_invalid")
    longest = max(width, height)
    if longest > 2600:
        scale = 2600 / longest
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    elif longest < 900:
        scale = min(2.0, 1800 / max(longest, 1))
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    gray = ImageOps.grayscale(image)
    return ImageOps.autocontrast(gray, cutoff=1)


def _confidence_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1.0
    return number


def _text_and_confidence(data: dict[str, list[Any]]) -> tuple[str, float | None]:
    lines: dict[tuple[int, int, int], list[tuple[str, float]]] = {}
    for index, raw_word in enumerate(data.get("text", [])):
        word = str(raw_word or "").strip()
        if not word:
            continue
        key = (
            int(data.get("block_num", [0])[index] or 0),
            int(data.get("par_num", [0])[index] or 0),
            int(data.get("line_num", [0])[index] or 0),
        )
        confidence = _confidence_value(data.get("conf", [-1])[index])
        lines.setdefault(key, []).append((word, confidence))
    output: list[str] = []
    confidences: list[float] = []
    for key in sorted(lines):
        words = lines[key]
        output.append(" ".join(word for word, _ in words))
        confidences.extend(value for _, value in words if value >= 0)
    average = round(sum(confidences) / len(confidences), 2) if confidences else None
    return "\n".join(output).strip(), average


def ocr_image(image: Any, *, page_segmentation: int = 6, timeout: int | None = None) -> tuple[str, float | None]:
    """Runs the verified local engine on an image, returning text and confidence."""
    engine = ocr_engine()
    if engine is None:
        raise DocumentProcessingError(
            "OCR is unavailable. Scanned documents cannot currently be processed.",
            category="ocr_unavailable",
            retryable=True,
        )
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover - requirements guarantee it
        raise DocumentProcessingError(
            "The Python OCR wrapper is not installed. Reinstall the backend requirements.",
            category="ocr_python_missing",
            retryable=True,
        ) from exc
    environment = os.environ.copy()
    if engine.tessdata_prefix:
        environment["TESSDATA_PREFIX"] = engine.tessdata_prefix
    pytesseract.pytesseract.tesseract_cmd = engine.command
    prepared = prepare_image(image)
    try:
        with _OCR_LOCK:
            data = pytesseract.image_to_data(
                prepared,
                lang=engine.language_argument,
                config=f"--psm {page_segmentation}",
                output_type=pytesseract.Output.DICT,
                timeout=timeout or settings.document_ocr_page_timeout,
            )
    except (pytesseract.TesseractError, RuntimeError, OSError) as exc:
        raise DocumentProcessingError(
            "Local OCR failed on a page. Check the OCR installation and try again.",
            category="ocr_failed",
            retryable=True,
        ) from exc
    text, confidence = _text_and_confidence(data)
    return text, confidence


_IDENTIFIER_REFINE_HINT = re.compile(
    r"\b(?:pan|aadha?ar|adhar|uidai|\buid\b|dob|date\s+of\s+birth|father|mother|board|marks|roll)\b",
    re.IGNORECASE,
)


def should_refine_ocr(text: str, confidence: float | None) -> bool:
    """Whether a low-confidence page with identifier labels merits one focused retry."""
    return bool(
        confidence is not None
        and confidence < 45
        and _IDENTIFIER_REFINE_HINT.search(text or "")
    )


def prefer_ocr_result(
    old_text: str,
    old_confidence: float | None,
    new_text: str,
    new_confidence: float | None,
) -> bool:
    """Accepts a recovery pass only when it is meaningfully better and complete."""
    if not new_text or new_confidence is None:
        return False
    if old_confidence is not None and new_confidence < old_confidence + 5:
        return False
    if old_text and len(new_text) < len(old_text) * 0.6:
        return False
    return True


# ---------------------------------------------------------------------------
# Page-level extraction
# ---------------------------------------------------------------------------


@dataclass
class PageResult:
    page_number: int | None
    text: str = ""
    section_heading: str | None = None
    method: str = "none"
    status: str = "failed"
    ocr_confidence: float | None = None
    error_category: str | None = None
    error_detail: str | None = None
    duration_ms: int = 0

    def as_row(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "text": self.text,
            "section_heading": self.section_heading,
            "method": self.method,
            "status": self.status,
            "ocr_confidence": self.ocr_confidence,
            "error_category": self.error_category,
            "error_detail": self.error_detail,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ExtractionOutcome:
    pages: list[PageResult]
    detected_pages: int
    method: str
    ocr_used: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def usable_pages(self) -> list[PageResult]:
        return [page for page in self.pages if page.status == "ok" and page.text.strip()]

    @property
    def failed_pages(self) -> list[PageResult]:
        return [page for page in self.pages if page.status != "ok"]

    @property
    def retryable_failure(self) -> bool:
        failures = self.failed_pages
        return bool(failures) and all(
            page.error_category in {"ocr_unavailable", "ocr_timeout", "ocr_failed", "ocr_page_limit", "ocr_python_missing"}
            for page in failures
        )


ProgressCallback = Callable[[str, int, int], None]


def _notify(progress: ProgressCallback | None, stage: str, done: int, total: int) -> None:
    if progress is not None:
        try:
            progress(stage, done, total)
        except Exception:  # pragma: no cover - progress must never break processing
            pass


def _pdf_page_text(page: Any) -> str:
    try:
        text = page.get_text("text") or ""
    except (RuntimeError, ValueError):
        return ""
    return str(text)


def _extraction_method(pages: list[PageResult]) -> str:
    methods = {page.method for page in pages if page.status == "ok"}
    if methods == {"ocr"}:
        return "pdf_ocr"
    if methods == {"native"}:
        return "pdf_text"
    if "ocr" in methods:
        return "pdf_text+ocr"
    return "pdf_none"


def extract_pdf(content: bytes, *, progress: ProgressCallback | None = None) -> ExtractionOutcome:
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - legacy PyMuPDF import name
        import fitz  # type: ignore

    from PIL import Image

    try:
        document = fitz.open(stream=content, filetype="pdf")
    except (RuntimeError, ValueError, OSError) as exc:
        raise DocumentProcessingError("The PDF could not be rendered.", category="pdf_render_failed") from exc
    try:
        if getattr(document, "needs_pass", False):
            raise DocumentProcessingError(
                "Password-protected PDFs are not supported.",
                category="pdf_password_protected",
            )
        total = int(document.page_count)
        if total <= 0:
            raise DocumentProcessingError("The PDF contains no pages.", category="pdf_empty")
        if total > settings.document_max_pdf_pages:
            raise DocumentProcessingError(
                f"PDFs are limited to {settings.document_max_pdf_pages} pages for local processing.",
                category="pdf_page_limit",
            )
        engine = ocr_engine()
        pages: list[PageResult] = []
        ocr_pages_used = 0
        started = time.monotonic()
        ocr_block_reason: str | None = None
        for index in range(total):
            page_started = time.monotonic()
            page = document[index]
            native = _pdf_page_text(page)
            if len(" ".join(native.split())) >= settings.document_native_text_min_chars:
                pages.append(
                    PageResult(
                        page_number=index + 1,
                        text=native.strip(),
                        method="native",
                        status="ok",
                        duration_ms=int((time.monotonic() - page_started) * 1000),
                    )
                )
                _notify(progress, "text_extracting", index + 1, total)
                continue
            if engine is None:
                pages.append(
                    PageResult(
                        page_number=index + 1,
                        text=native.strip(),
                        method="none",
                        status="failed",
                        error_category="ocr_unavailable",
                        error_detail="OCR engine unavailable; this page needs OCR.",
                        duration_ms=int((time.monotonic() - page_started) * 1000),
                    )
                )
                _notify(progress, "ocr_processing", index + 1, total)
                continue
            if ocr_pages_used >= settings.document_ocr_max_pages:
                ocr_block_reason = "ocr_page_limit"
            elif time.monotonic() - started > settings.document_ocr_total_timeout:
                ocr_block_reason = "ocr_timeout"
            if ocr_block_reason:
                pages.append(
                    PageResult(
                        page_number=index + 1,
                        text=native.strip(),
                        method="none",
                        status="partial" if native.strip() else "failed",
                        error_category=ocr_block_reason,
                        error_detail=(
                            f"OCR was limited to {settings.document_ocr_max_pages} pages."
                            if ocr_block_reason == "ocr_page_limit"
                            else "The document exceeded its OCR processing time budget."
                        ),
                        duration_ms=int((time.monotonic() - page_started) * 1000),
                    )
                )
                _notify(progress, "ocr_processing", index + 1, total)
                continue
            try:
                pixmap = page.get_pixmap(dpi=settings.document_ocr_dpi, alpha=False)
                if pixmap.width * pixmap.height > 40_000_000:
                    raise DocumentProcessingError(
                        "A PDF page is too large for safe OCR.", category="page_too_large"
                    )
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                text, confidence = ocr_image(image, timeout=settings.document_ocr_page_timeout)
                if should_refine_ocr(text, confidence):
                    try:
                        refined_dpi = min(500, settings.document_ocr_dpi + 150)
                        refined_pixmap = page.get_pixmap(dpi=refined_dpi, alpha=False)
                        if refined_pixmap.width * refined_pixmap.height <= 40_000_000:
                            refined_image = Image.open(io.BytesIO(refined_pixmap.tobytes("png")))
                            refined_text, refined_confidence = ocr_image(
                                refined_image, timeout=settings.document_ocr_page_timeout
                            )
                            if prefer_ocr_result(text, confidence, refined_text, refined_confidence):
                                text, confidence = refined_text, refined_confidence
                    except (DocumentProcessingError, RuntimeError, ValueError, OSError):
                        # Keep the first verified pass when a focused retry fails.
                        pass
            except DocumentProcessingError as exc:
                pages.append(
                    PageResult(
                        page_number=index + 1,
                        text=native.strip(),
                        method="none",
                        status="failed",
                        error_category=exc.category,
                        error_detail=exc.message,
                        duration_ms=int((time.monotonic() - page_started) * 1000),
                    )
                )
                _notify(progress, "ocr_processing", index + 1, total)
                continue
            except (RuntimeError, ValueError, OSError) as exc:
                pages.append(
                    PageResult(
                        page_number=index + 1,
                        text=native.strip(),
                        method="none",
                        status="failed",
                        error_category="pdf_render_failed",
                        error_detail="This page could not be rendered for OCR.",
                        duration_ms=int((time.monotonic() - page_started) * 1000),
                    )
                )
                _notify(progress, "ocr_processing", index + 1, total)
                continue
            ocr_pages_used += 1
            pages.append(
                PageResult(
                    page_number=index + 1,
                    text=text,
                    method="ocr" if text else "none",
                    status="ok" if text else "failed",
                    ocr_confidence=confidence,
                    error_category=None if text else "ocr_no_text",
                    error_detail=None if text else "OCR found no readable text on this page.",
                    duration_ms=int((time.monotonic() - page_started) * 1000),
                )
            )
            _notify(progress, "ocr_processing", index + 1, total)
        warnings = []
        if engine is None:
            warnings.append("OCR engine unavailable; scanned pages were not read.")
        else:
            missing = [language for language in engine.requested_languages if language in engine.missing_languages]
            if missing:
                labels = ", ".join(LANGUAGE_LABELS.get(language, language) for language in missing)
                warnings.append(f"Optional OCR language data missing: {labels}.")
        outcome = ExtractionOutcome(
            pages=pages,
            detected_pages=total,
            method=_extraction_method(pages),
            ocr_used=any(page.method == "ocr" for page in pages),
            warnings=warnings,
        )
        if not outcome.usable_pages:
            if engine is None:
                raise DocumentProcessingError(
                    f"{ocr_error_message()} Scanned pages could not be read; text extraction found nothing usable.",
                    category="ocr_unavailable",
                    retryable=True,
                    pages=pages,
                    detected_pages=total,
                )
            raise DocumentProcessingError(
                "No readable text was found in the PDF after text extraction and OCR.",
                category="no_readable_text",
                retryable=outcome.retryable_failure,
                pages=pages,
                detected_pages=total,
            )
        return outcome
    finally:
        document.close()


def extract_image(extension: str, content: bytes, *, progress: ProgressCallback | None = None) -> ExtractionOutcome:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(content))
        expected = EXPECTED_IMAGE_FORMATS.get(extension, set())
        if expected and image.format not in expected:
            raise DocumentProcessingError(
                "The image content does not match its file extension.", category="image_type_mismatch"
            )
        image.load()
        if image.width * image.height > 40_000_000:
            raise DocumentProcessingError(
                "Images are limited to 40 megapixels for local OCR.", category="image_too_large"
            )
    except DocumentProcessingError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise DocumentProcessingError("The image is corrupt or unreadable.", category="image_corrupt") from exc

    total = max(1, getattr(image, "n_frames", 1))
    pages: list[PageResult] = []
    for frame in range(total):
        started = time.monotonic()
        try:
            if total > 1:
                image.seek(frame)
            frame_image = image.copy()
            text, confidence = ocr_image(frame_image, page_segmentation=3)
            if should_refine_ocr(text, confidence) and frame_image.width * frame_image.height * 4 <= 40_000_000:
                try:
                    larger = frame_image.resize(
                        (frame_image.width * 2, frame_image.height * 2)
                    )
                    refined_text, refined_confidence = ocr_image(larger, page_segmentation=6)
                    if prefer_ocr_result(text, confidence, refined_text, refined_confidence):
                        text, confidence = refined_text, refined_confidence
                except (DocumentProcessingError, RuntimeError, ValueError, OSError):
                    pass
            pages.append(
                PageResult(
                    page_number=frame + 1,
                    text=text,
                    method="ocr" if text else "none",
                    status="ok" if text else "failed",
                    ocr_confidence=confidence,
                    error_category=None if text else "ocr_no_text",
                    error_detail=None if text else "OCR found no readable text in this image page.",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except DocumentProcessingError as exc:
            pages.append(
                PageResult(
                    page_number=frame + 1,
                    status="failed",
                    error_category=exc.category,
                    error_detail=exc.message,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
        _notify(progress, "ocr_processing", frame + 1, total)
    outcome = ExtractionOutcome(pages=pages, detected_pages=total, method="image_ocr", ocr_used=any(p.text for p in pages))
    if not outcome.usable_pages:
        if ocr_engine() is None:
            raise DocumentProcessingError(
                f"{ocr_error_message()} The image could not be read.",
                category="ocr_unavailable",
                retryable=True,
                pages=pages,
                detected_pages=total,
            )
        raise DocumentProcessingError(
            "Local OCR found no readable text in the image.",
            category="no_readable_text",
            retryable=outcome.retryable_failure,
            pages=pages,
            detected_pages=total,
        )
    return outcome


# ---------------------------------------------------------------------------
# Structured exact-field extraction (deterministic grounding)
# ---------------------------------------------------------------------------

PAN_PATTERN = re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])")
AADHAAR_PATTERN = re.compile(r"(?<!\d)(\d{4}[\s-]?\d{4}[\s-]?\d{4})(?!\d)")
DOB_LABEL_PATTERN = re.compile(
    r"(?:date\s*of\s*birth|d\.?o\.?b\.?|dob|birth\s*date|जन्म\s*तिथि|जन्म\s*तारीख)\s*[:\-–]?\s*([0-3]?\d[\s./-][01]?\d[\s./-](?:19|20)?\d{2})",
    re.IGNORECASE,
)
PAN_LABEL_CONTEXT = re.compile(r"pan|permanent\s+account\s+number|income\s+tax", re.IGNORECASE)
AADHAAR_LABEL_CONTEXT = re.compile(r"aadha?ar|adhar|aashar|uidai|\buid\b|unique\s+identification", re.IGNORECASE)


def normalize_field_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def extract_pan_candidates(text: str) -> list[str]:
    normalized = (text or "").upper()
    return list(dict.fromkeys(match.group(0) for match in PAN_PATTERN.finditer(normalized)))


def extract_aadhaar_candidates(text: str) -> list[str]:
    found: list[str] = []
    for match in AADHAAR_PATTERN.finditer(text or ""):
        raw = match.group(1)
        digits = re.sub(r"\D", "", raw)
        if len(digits) != 12 or digits[0] in {"0", "1"}:
            continue
        found.append(raw.strip())
    return list(dict.fromkeys(found))


def extract_dob_candidates(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(1).strip() for match in DOB_LABEL_PATTERN.finditer(text or "")))


def _field_answer(
    field_name: str,
    values: list[tuple[str, str, int | None, float | None]],
) -> dict[str, Any] | None:
    unique: dict[str, list[tuple[str, str, int | None, float | None]]] = {}
    for value, document_name, page_number, confidence in values:
        unique.setdefault(value, []).append((value, document_name, page_number, confidence))
    if not unique:
        return None
    ordered = sorted(unique, key=lambda value: -len(unique[value]))
    best = ordered[0]
    evidence = unique[best][0]
    confidence = evidence[3]
    return {
        "field": field_name,
        "value": best,
        "document_name": evidence[1],
        "page_number": evidence[2],
        "confidence": confidence,
        "low_confidence": confidence is not None and confidence < 60,
        "ambiguous": len(ordered) > 1,
        "alternatives": [
            {
                "value": value,
                "document_name": unique[value][0][1],
                "page_number": unique[value][0][2],
                "confidence": unique[value][0][3],
            }
            for value in ordered[1:4]
        ],
    }


def extract_exact_field(query: str, sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Deterministically extracts PAN/Aadhaar/DOB evidence from retrieved passages."""
    folded = (query or "").casefold()
    wants_pan = bool(re.search(r"\bpan\b|permanent\s+account\s+number", folded))
    wants_aadhaar = bool(re.search(r"aadha?ar|adhar|aashar|\buid\b", folded))
    wants_dob = bool(re.search(r"\b(?:dob|date\s+of\s+birth|birth\s+date|born)\b", folded))
    if not (wants_pan or wants_aadhaar or wants_dob):
        return None
    pan_values: list[tuple[str, str, int | None, float | None]] = []
    aadhaar_values: list[tuple[str, str, int | None, float | None]] = []
    dob_values: list[tuple[str, str, int | None, float | None]] = []
    for source in sources:
        text = f"{source.get('excerpt') or ''}\n{source.get('snippet') or ''}"
        name = str(source.get("filename") or source.get("document_name") or "Document")
        page = source.get("page_number")
        confidence = source.get("ocr_confidence")
        for value in extract_pan_candidates(text):
            pan_values.append((value, name, page, confidence))
        for value in extract_aadhaar_candidates(text):
            aadhaar_values.append((value, name, page, confidence))
        for value in extract_dob_candidates(text):
            dob_values.append((value, name, page, confidence))
    if wants_pan and pan_values:
        return _field_answer("PAN number", pan_values)
    if wants_aadhaar and aadhaar_values:
        return _field_answer("Aadhaar number", aadhaar_values)
    if wants_dob and dob_values:
        return _field_answer("date of birth", dob_values)
    return None


def field_label_evidence(query: str, sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reports that a requested field label exists but no valid value was read.

    This lets the assistant say the page needs visual verification instead of
    claiming the information is absent or guessing a number.
    """
    folded = (query or "").casefold()
    checks = (
        (r"\bpan\b|permanent\s+account\s+number", r"\bpan\b|permanent\s+account\s+number", "PAN number"),
        (r"aadha?ar|adhar|aashar|\buid\b", r"aadha?ar|adhar|aashar|uidai|\buid\b", "Aadhaar number"),
        (r"\b(?:dob|date\s+of\s+birth|birth\s+date|born)\b", r"date\s+of\s+birth|d\.?o\.?b\.?|\bdob\b|birth\s*date", "date of birth"),
    )
    requested = next((item for item in checks if re.search(item[0], folded)), None)
    if requested is None:
        return None
    for source in sources:
        text = f"{source.get('excerpt') or ''}\n{source.get('snippet') or ''}"
        if re.search(requested[1], text, re.IGNORECASE):
            return {
                "field": requested[2],
                "document_name": str(source.get("filename") or source.get("document_name") or "Document"),
                "document_id": source.get("document_id"),
                "page_number": source.get("page_number"),
                "ocr_confidence": source.get("ocr_confidence"),
            }
    return None


def field_is_uncertain(field: dict[str, Any] | None) -> bool:
    return bool(
        field
        and (
            field.get("ambiguous")
            or field.get("low_confidence")
            or field.get("page_number") is None
        )
    )


# ---------------------------------------------------------------------------
# Health, diagnostics and consistency
# ---------------------------------------------------------------------------


def mark_active(document_id: str, stage: str, pages_done: int = 0, pages_total: int = 0) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_DOCUMENTS[document_id] = {
            "stage": stage,
            "pages_done": pages_done,
            "pages_total": pages_total,
            "started_at": _ACTIVE_DOCUMENTS.get(document_id, {}).get("started_at") or time.time(),
        }


def clear_active(document_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_DOCUMENTS.pop(document_id, None)


def active_documents() -> list[dict[str, Any]]:
    with _ACTIVE_LOCK:
        return [
            {"document_id": document_id, **{k: v for k, v in values.items() if k != "started_at"}}
            for document_id, values in _ACTIVE_DOCUMENTS.items()
        ]


def _library_check() -> tuple[bool, str]:
    try:
        import pypdf  # noqa: F401
        import pymupdf  # noqa: F401
        from PIL import Image  # noqa: F401

        return True, "PDF rendering and image processing libraries are available."
    except ImportError as exc:
        return False, f"A required PDF/image library is missing ({exc.name})."


def _storage_check() -> tuple[bool, str]:
    try:
        settings.documents_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.documents_dir / ".write-probe"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return True, "The document storage folder is writable."
    except OSError:
        return False, "The document storage folder is not writable."


def pipeline_health(connection: Any, *, force: bool = False) -> dict[str, Any]:
    """Documented, non-sensitive health summary for the owner."""
    from . import crypto

    resolution = resolve_ocr(force=force)
    engine = resolution.engine
    usable = engine is not None and engine.probe_passed
    libraries_ok, libraries_message = _library_check()
    storage_ok, storage_message = _storage_check()
    counts = {
        "documents": 0,
        "ready": 0,
        "partial": 0,
        "failed": 0,
        "retry_required": 0,
        "active": 0,
    }
    last_success = None
    try:
        row = connection.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN status = 'indexed' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'retry_required' THEN 1 ELSE 0 END), "
            "MAX(CASE WHEN status IN ('indexed', 'partial') THEN last_processed_at END) "
            "FROM documents"
        ).fetchone()
        if row:
            counts["documents"] = int(row[0] or 0)
            counts["ready"] = int(row[1] or 0)
            counts["partial"] = int(row[2] or 0)
            counts["failed"] = int(row[3] or 0)
            counts["retry_required"] = int(row[4] or 0)
            last_success = row[5]
        counts["active"] = len(active_documents())
        queued = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE status = 'processing'"
        ).fetchone()
        retry = connection.execute(
            "SELECT COUNT(DISTINCT document_id) FROM document_processing_jobs WHERE status = 'interrupted'"
        ).fetchone()
        counts["stale_jobs"] = int(retry[0] or 0)
        counts["queued"] = int(queued[0] or 0)
    except Exception:  # pragma: no cover - diagnostics must never break the page
        counts.setdefault("stale_jobs", 0)
        counts.setdefault("queued", 0)
    missing_labels = (
        [LANGUAGE_LABELS.get(language, language) for language in engine.missing_languages]
        if engine
        else []
    )
    ready = bool(usable and libraries_ok and storage_ok)
    return {
        "ready": ready,
        "ocr": {
            "available": bool(usable),
            "version": engine.version if engine else None,
            "source": engine.source if engine else None,
            "executable_configured": bool(settings.tesseract_cmd),
            "tessdata_prefix": engine.tessdata_prefix if engine else None,
            "languages": list(engine.languages) if engine else [],
            "requested_languages": list(engine.requested_languages) if engine else settings.requested_ocr_languages,
            "usable_languages": list(engine.usable_languages) if engine else [],
            "missing_languages": list(engine.missing_languages) if engine else [],
            "missing_language_labels": missing_labels,
            "probe_passed": bool(engine and engine.probe_passed),
            "probe_detail": engine.probe_detail if engine else None,
            "errors": resolution.errors,
            "message": (
                "OCR engine verified: version and language data are available."
                if usable
                else (engine.probe_detail if engine else (resolution.errors[0]["message"] if resolution.errors else "OCR engine not found."))
            ),
            "action": (
                None
                if usable
                else "Run scripts/setup-document-ocr-windows.ps1 (or set TESSERACT_CMD) and restart the backend."
            ),
        },
        "pdf_rendering": {"available": libraries_ok, "message": libraries_message},
        "image_processing": {"available": libraries_ok, "message": libraries_message},
        "storage": {
            "available": storage_ok,
            "message": storage_message,
            "encryption": "aes-256-gcm" if crypto.encryption_enabled() else "not configured",
        },
        "worker": {
            "mode": "inline-with-persistent-jobs",
            "active": active_documents(),
            "active_count": counts["active"],
            "queued": counts.get("queued", 0),
            "stale_jobs": counts.get("stale_jobs", 0),
            "concurrency": 1,
        },
        "documents": counts,
        "last_successful_processing": last_success,
        "limits": {
            "max_pdf_pages": settings.document_max_pdf_pages,
            "ocr_max_pages": settings.document_ocr_max_pages,
            "ocr_dpi": settings.document_ocr_dpi,
            "page_timeout_seconds": settings.document_ocr_page_timeout,
            "total_ocr_timeout_seconds": settings.document_ocr_total_timeout,
        },
    }


def consistency_report(connection: Any) -> dict[str, Any]:
    """Read-only integrity checks across documents, pages, chunks and jobs."""
    issues: list[dict[str, Any]] = []
    document_root = settings.documents_dir

    def add(kind: str, severity: str, message: str, document_id: str | None = None) -> None:
        issues.append({"kind": kind, "severity": severity, "message": message, "document_id": document_id})

    documents = connection.execute(
        "SELECT id, display_name, original_name, storage_name, status, chunk_count, page_count, "
        "processed_pages, failed_pages FROM documents"
    ).fetchall()
    for document in documents:
        document_id = document["id"]
        name = document["display_name"] or document["original_name"] or "Document"
        if document["status"] == "indexed" and int(document["chunk_count"] or 0) <= 0:
            add("ready_without_chunks", "error", f"{name} is marked ready but has no searchable chunks.", document_id)
        chunks = connection.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?", (document_id,)
        ).fetchone()[0]
        if int(chunks) != int(document["chunk_count"] or 0):
            add("chunk_count_mismatch", "warning", f"{name} reports {document['chunk_count']} chunks but stores {chunks}.", document_id)
        pages = connection.execute(
            "SELECT COUNT(*) FROM document_pages WHERE document_id = ? AND status = 'ok' AND TRIM(content) <> ''",
            (document_id,),
        ).fetchone()[0]
        if document["status"] in {"indexed", "partial"} and pages == 0:
            add("missing_pages", "error", f"{name} has no usable extracted pages.", document_id)
        duplicates = connection.execute(
            "SELECT page_number, COUNT(*) FROM document_pages WHERE document_id = ? "
            "AND page_number IS NOT NULL GROUP BY page_number HAVING COUNT(*) > 1 LIMIT 5",
            (document_id,),
        ).fetchall()
        for row in duplicates:
            add("duplicate_pages", "warning", f"{name} stores page {row[0]} more than once.", document_id)
        storage_name = document["storage_name"]
        if not storage_name or not (document_root / storage_name).exists():
            add("missing_original", "error", f"The original file for {name} is missing.", document_id)
        if document["status"] == "processing":
            add("stale_processing", "warning", f"{name} is marked processing; it may be interrupted.", document_id)
        if document["status"] in {"retry_required", "failed"} and int(document["chunk_count"] or 0) > 0:
            add("retry_with_chunks", "warning", f"{name} is not ready but still has indexed chunks.", document_id)
    stale_jobs = connection.execute(
        "SELECT document_id FROM document_processing_jobs WHERE status IN ('running', 'interrupted')"
    ).fetchall()
    for job in stale_jobs:
        add("stale_job", "warning", "A processing job did not report a final result.", job["document_id"])
    orphan_chunks = connection.execute(
        "SELECT COUNT(*) FROM document_chunks c LEFT JOIN documents d ON d.id = c.document_id WHERE d.id IS NULL"
    ).fetchone()[0]
    if orphan_chunks:
        add("orphan_chunks", "warning", f"{orphan_chunks} index chunks are not linked to a document.")
    return {
        "issues": issues,
        "issue_count": len(issues),
        "errors": sum(1 for issue in issues if issue["severity"] == "error"),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def startup_summary() -> str:
    """Short, non-sensitive startup log line about document-processing readiness."""
    resolution = resolve_ocr()
    engine = resolution.engine
    if engine is not None and engine.probe_passed:
        languages = ", ".join(engine.usable_languages)
        note = ""
        if engine.missing_languages:
            note = f"; optional language data missing: {', '.join(engine.missing_languages)}"
        return f"Document processing ready: Tesseract {engine.version} ({languages}){note}."
    reason = engine.probe_detail if engine is not None else (
        resolution.errors[0]["message"] if resolution.errors else "OCR engine not found."
    )
    return (
        "Document processing requires setup: "
        f"{reason} Text-only PDFs and office documents still work; scanned pages will require OCR. "
        "Run scripts/setup-document-ocr-windows.ps1 and restart the backend."
    )


def ocr_self_test() -> dict[str, Any]:
    """Used by the setup/diagnostic scripts; never touches owner documents."""
    resolution = resolve_ocr(force=True)
    engine = resolution.engine
    result: dict[str, Any] = {
        "available": bool(engine and engine.probe_passed),
        "version": engine.version if engine else None,
        "languages": list(engine.languages) if engine else [],
        "usable_languages": list(engine.usable_languages) if engine else [],
        "missing_languages": list(engine.missing_languages) if engine else [],
        "executable": engine.command if engine else None,
        "source": engine.source if engine else None,
        "probe_detail": engine.probe_detail if engine else None,
        "errors": resolution.errors,
    }
    return result


if __name__ == "__main__":  # pragma: no cover - manual diagnostics helper
    import json

    print(json.dumps(ocr_self_test(), indent=2))
