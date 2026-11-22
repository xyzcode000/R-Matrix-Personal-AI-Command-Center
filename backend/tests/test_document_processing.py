"""Page-level processing, OCR failure/recovery, retry and grounding tests.

All fixtures are synthetic. No real identity numbers are used anywhere.
"""

from __future__ import annotations

import pymupdf
import pytest

from app import database, document_pipeline, documents
from app.config import settings

FAKE_PAN = "ABCDE1234F"
FAKE_AADHAAR = "2345 6789 0123"


@pytest.fixture(autouse=True)
def _allow_synthetic_uploads(client, monkeypatch):
    # The shared client fixture keeps a tiny limit; synthetic PDFs need a little room.
    # Depending on ``client`` makes this run after the shared fixture configures settings.
    monkeypatch.setattr(settings, "max_upload_bytes", 2_000_000)


def fake_engine(source: str = "test") -> document_pipeline.OcrEngine:
    return document_pipeline.OcrEngine(
        command="fake-tesseract",
        version="5.0.0-test",
        languages=("eng", "osd"),
        source=source,
        tessdata_prefix=None,
        requested_languages=("eng",),
        usable_languages=("eng",),
        missing_languages=(),
        probe_passed=True,
        probe_detail="synthetic test engine",
    )


def ocr_available(monkeypatch, text: str = "Synthetic OCR text for a scanned page.") -> None:
    monkeypatch.setattr(document_pipeline, "ocr_engine", lambda **_kwargs: fake_engine())
    monkeypatch.setattr(
        document_pipeline,
        "ocr_image",
        lambda *_args, **_kwargs: (text, 92.0),
    )


def ocr_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(document_pipeline, "ocr_engine", lambda **_kwargs: None)
    monkeypatch.setattr(
        document_pipeline,
        "ocr_error_message",
        lambda: "The local OCR engine (Tesseract) is not available.",
    )


def pdf_bytes(*pages: str) -> bytes:
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_text((40, 90), text, fontsize=12)
    output = document.tobytes()
    document.close()
    return output


def upload(client, filename: str, content: bytes, content_type: str, **form):
    return client.post(
        "/api/documents/upload",
        files={"file": (filename, content, content_type)},
        data=form,
    )


def test_mixed_pdf_processes_every_page_and_records_methods(client, monkeypatch):
    ocr_available(monkeypatch, "Scanned page content: instruction reference 7788.")
    content = pdf_bytes(
        "This first page has a complete native text layer for reliable extraction.",
        "",
        "This third page also contains a native text layer that is long enough.",
    )
    response = upload(
        client,
        "mixed.pdf",
        content,
        "application/pdf",
        display_name="Mixed document",
        category="Education",
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "indexed"
    assert document["page_count"] == 3
    assert document["processed_pages"] == 3
    assert document["failed_pages"] == 0
    assert document["ocr_status"] == "used"
    assert document["chunk_count"] > 0

    pages = client.get(f"/api/documents/{document['id']}/pages").json()["pages"]
    assert [page["page_number"] for page in pages] == [1, 2, 3]
    assert [page["method"] for page in pages] == ["native", "ocr", "native"]
    assert all(page["status"] == "ok" for page in pages)
    assert pages[1]["ocr_confidence"] == 92.0


def test_scanned_pdf_without_ocr_is_retry_required_then_recovers_idempotently(client, monkeypatch):
    ocr_unavailable(monkeypatch)
    content = pdf_bytes("", "")
    response = upload(client, "scan.pdf", content, "application/pdf", display_name="Scanned identity")
    assert response.status_code == 201, response.text
    failed = response.json()
    assert failed["status"] == "retry_required"
    assert failed["chunk_count"] == 0
    assert failed["failed_pages"] == 2
    assert "Tesseract" in (failed["processing_error"] or "")
    assert client.post("/api/documents/search", json={"query": "scanned identity"}).json() == []

    # A retry before the dependency is ready stays recoverable, not "ready".
    still_missing = client.post(f"/api/documents/{failed['id']}/retry")
    assert still_missing.status_code == 200
    assert still_missing.json()["status"] == "retry_required"

    ocr_available(monkeypatch, "Recovered scanned text with reference 4455.")
    recovered = client.post(f"/api/documents/{failed['id']}/retry")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "indexed"
    chunks = recovered.json()["chunk_count"]
    assert chunks > 0

    # Idempotent: a second retry replaces, never appends, the index.
    again = client.post(f"/api/documents/{failed['id']}/retry")
    assert again.status_code == 200
    assert again.json()["chunk_count"] == chunks
    with database.session() as connection:
        pages = connection.execute(
            "SELECT COUNT(*) FROM document_pages WHERE document_id = ?", (failed["id"],)
        ).fetchone()[0]
        stored_chunks = connection.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?", (failed["id"],)
        ).fetchone()[0]
    assert pages == 2
    assert stored_chunks == chunks


def test_partial_processing_preserves_usable_pages_without_false_ready(client, monkeypatch):
    document_pipeline.reset_ocr_cache()

    def engine(**_kwargs):
        return fake_engine()

    calls = {"count": 0}

    def flaky_ocr(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise document_pipeline.DocumentProcessingError(
                "Local OCR failed on a page.", category="ocr_failed", retryable=True
            )
        return "Readable scanned page text for the second usable page.", 85.0

    monkeypatch.setattr(document_pipeline, "ocr_engine", engine)
    monkeypatch.setattr(document_pipeline, "ocr_image", flaky_ocr)
    content = pdf_bytes(
        "Native first page with enough characters to skip local OCR entirely.",
        "",
        "",
    )
    response = upload(client, "partial.pdf", content, "application/pdf", display_name="Partial scan")
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "partial"
    assert result["page_count"] == 3
    assert result["processed_pages"] == 2
    assert result["failed_pages"] == 1
    assert "need retry" in (result["processing_error"] or "").casefold()
    assert result["chunk_count"] > 0
    assert client.post("/api/documents/search", json={"query": "readable scanned page"}).json()


def test_rebuild_index_only_uses_existing_extraction(client, monkeypatch):
    ocr_available(monkeypatch)
    content = pdf_bytes("", "")
    response = upload(client, "scan.pdf", content, "application/pdf", display_name="Scanned notes")
    document = response.json()
    assert document["status"] == "indexed"
    rebuilt = client.post(f"/api/documents/{document['id']}/rebuild-index")
    assert rebuilt.status_code == 200
    assert rebuilt.json()["chunk_count"] == document["chunk_count"]

    empty = upload(client, "empty-text.txt", b"x", "text/plain")
    # A document that cannot be read has no pages to rebuild.
    client.post(f"/api/documents/{empty.json()['id']}/retry")
    # Text documents are valid; the rebuild remains available and idempotent.
    assert client.post(f"/api/documents/{empty.json()['id']}/rebuild-index").status_code == 200


def test_health_and_diagnostics_never_expose_document_text(client):
    upload(
        client,
        "private.txt",
        b"Synthetic private content with a fake token REF-9001.",
        "text/plain",
        display_name="Private reference",
    )
    health = client.get("/api/documents/health")
    assert health.status_code == 200
    payload = health.json()
    assert "ready" in payload
    assert payload["ocr"]["probe_passed"] in {True, False}
    assert payload["limits"]["ocr_max_pages"] >= 1
    assert "REF-9001" not in health.text
    assert "Synthetic private content" not in health.text

    diagnostics = client.get("/api/documents/diagnostics")
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert body["issue_count"] == body["errors"] + sum(
        1 for issue in body["issues"] if issue["severity"] != "error"
    )
    assert "Synthetic private content" not in diagnostics.text


def test_exact_field_answers_and_empty_source_behaviour(client):
    upload(
        client,
        "identity.txt",
        (
            f"PAN: {FAKE_PAN}\n"
            f"Aadhaar: {FAKE_AADHAAR}\n"
            "Date of Birth: 01/01/2000\n"
        ).encode("utf-8"),
        "text/plain",
        display_name="Synthetic identity card",
        category="Identity",
    )

    pan = client.post("/api/chat", json={"message": "What is my PAN number?"})
    assert pan.status_code == 200, pan.text
    pan_result = pan.json()["message"]
    assert FAKE_PAN in pan_result["content"]
    assert "Synthetic identity card" in pan_result["content"]
    assert len(pan_result["sources"]) == 1
    assert pan_result["sources"][0]["document_name"] == "Synthetic identity card"

    dob = client.post("/api/chat", json={"message": "What is my date of birth?"})
    assert "01/01/2000" in dob.json()["message"]["content"]

    missing = client.post(
        "/api/chat",
        json={"message": "What is my voter registration number in my documents?"},
    )
    assert missing.status_code == 200, missing.text
    missing_result = missing.json()["message"]
    assert missing_result["content"] == "I couldn't find that information in your uploaded documents."
    assert missing_result["sources"] == []
    # Previous message sources never leak into this one.
    assert pan_result["sources"]


def test_unreadable_document_is_explained_instead_of_claiming_absence(client, monkeypatch):
    ocr_unavailable(monkeypatch)
    upload(
        client,
        "licence.pdf",
        pdf_bytes(""),
        "application/pdf",
        display_name="Driving licence",
        category="Identity",
    )
    response = client.post("/api/chat", json={"message": "What is my driving licence number?"})
    assert response.status_code == 200, response.text
    result = response.json()["message"]
    assert "haven't been able to read" in result["content"]
    assert len(result["sources"]) == 1
    assert result["sources"][0]["document_name"] == "Driving licence"
    assert result["sources"][0]["page_number"] is None


def test_structured_field_extraction_rejects_invalid_candidates():
    assert document_pipeline.extract_pan_candidates("PAN ABCDE1234F and lower abcde1234f") == [FAKE_PAN]
    assert document_pipeline.extract_pan_candidates("ABCD1234F") == []
    assert document_pipeline.extract_aadhaar_candidates("Aadhaar 2345 6789 0123") == [FAKE_AADHAAR]
    assert document_pipeline.extract_aadhaar_candidates("0123 4567 8901") == []
    assert document_pipeline.extract_dob_candidates("Date of Birth: 02/03/1999") == ["02/03/1999"]
    assert document_pipeline.extract_dob_candidates("Issue Date: 02/03/1999") == []

    sources = [
        {
            "filename": "Synthetic identity card",
            "page_number": 2,
            "excerpt": f"PAN: {FAKE_PAN}",
        }
    ]
    field = documents.extract_exact_field("What is my PAN number?", sources)
    assert field and field["value"] == FAKE_PAN and field["page_number"] == 2
    assert documents.extract_exact_field("What is my passport number?", sources) is None


def test_interrupted_jobs_become_recoverable_after_restart(client):
    document = upload(
        client, "recover.txt", b"Recoverable synthetic content.", "text/plain", display_name="Recovery check"
    ).json()
    with database.session() as connection:
        connection.execute(
            "UPDATE documents SET status = 'processing', processing_stage = 'ocr_processing' WHERE id = ?",
            (document["id"],),
        )
        connection.execute(
            "INSERT INTO document_processing_jobs (id, document_id, kind, status, stage, attempts, started_at)"
            " VALUES ('stuck-job', ?, 'process', 'running', 'ocr_processing', 1, '2026-01-01T00:00:00+00:00')",
            (document["id"],),
        )
        connection.commit()
        recovered = documents.recover_interrupted_jobs(connection)
        assert recovered >= 1
        row = connection.execute(
            "SELECT status, processing_error FROM documents WHERE id = ?", (document["id"],)
        ).fetchone()
        job = connection.execute(
            "SELECT status FROM document_processing_jobs WHERE id = 'stuck-job'"
        ).fetchone()
    assert row["status"] == "retry_required"
    assert "interrupted" in row["processing_error"].casefold()
    assert job["status"] == "interrupted"


def test_document_health_requires_owner_authentication(anonymous_client):
    assert anonymous_client.get("/api/documents/health").status_code == 401
    assert anonymous_client.get("/api/documents/diagnostics").status_code == 401


def test_missing_original_file_does_not_invent_success(client, monkeypatch):
    ocr_available(monkeypatch)
    document = upload(
        client, "scan.pdf", pdf_bytes(""), "application/pdf", display_name="Original check"
    ).json()
    stored = database.settings.documents_dir / f"{document['id']}.pdf"
    stored.unlink()
    response = client.post(f"/api/documents/{document['id']}/retry")
    assert response.status_code == 404
    listed = client.get(f"/api/documents/{document['id']}").json()
    assert listed["status"] in {"failed", "retry_required"}
