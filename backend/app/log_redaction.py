"""Log-only masking of government identifier values.

The authenticated owner's chat responses are never masked; this module only
rewrites text on its way into application logs so PAN/Aadhaar values cannot be
persisted in log files or error text.
"""

from __future__ import annotations

import logging
import re

AADHAAR_ID = re.compile(r"(?<!\d)(?:\d{4}[\s-]?\d{4}[\s-]?\d{4})(?!\d)")
PAN_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Za-z0-9])")

REDACTED_AADHAAR = "AADHAAR:REDACTED"
REDACTED_PAN = "PAN:REDACTED"


def redact_government_ids(text: str | None) -> str:
    """Replaces PAN/Aadhaar-shaped values with a fixed placeholder."""
    if not text:
        return text or ""
    redacted = AADHAAR_ID.sub(REDACTED_AADHAAR, text)
    return PAN_ID.sub(REDACTED_PAN, redacted)


class GovernmentIdRedactionFilter(logging.Filter):
    """Logging filter that masks identifier formats in every rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never break the app
            return True
        masked = redact_government_ids(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


_LOGGER_NAMES = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi")


def install_log_redaction() -> None:
    """Attaches the filter once to the application and server loggers."""
    for name in _LOGGER_NAMES:
        target = logging.getLogger(name)
        if not any(isinstance(item, GovernmentIdRedactionFilter) for item in target.filters):
            target.addFilter(GovernmentIdRedactionFilter())
