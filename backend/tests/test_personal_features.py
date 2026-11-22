import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO

from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter

from app import database, documents, reminders
from app.config import settings


def test_office_and_csv_documents_extract_context(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 500_000)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Policy"
    sheet.append(["Name", "Expiry"])
    sheet.append(["Certificate", "2030-04-12"])
    xlsx = BytesIO()
    workbook.save(xlsx)

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1)).text = "Safety Procedures"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(5), Inches(1)).table
    table.cell(0, 0).text = "Condition"
    table.cell(0, 1).text = "Wear helmet"
    pptx = BytesIO()
    presentation.save(pptx)

    cases = [
        ("rows.csv", b"License,Expiry\nA-12,2030-04-12\n", "text/csv", "A-12"),
        ("policy.xlsx", xlsx.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "2030-04-12"),
        ("safety.pptx", pptx.getvalue(), "application/vnd.openxmlformats-officedocument.presentationml.presentation", "Wear helmet"),
    ]
    for filename, content, content_type, expected in cases:
        response = client.post(
            "/api/documents/upload",
            files={"file": (filename, content, content_type)},
        )
        assert response.status_code == 201, response.text
        assert response.json()["extraction_method"] in {"csv", "xlsx", "pptx"}
        content_response = client.get(f"/api/documents/{response.json()['id']}/content")
        assert content_response.status_code == 200
        assert expected in "\n".join(page["content"] for page in content_response.json()["pages"])


def _fake_engine():
    from app.document_pipeline import OcrEngine

    return OcrEngine(
        command="fake-tesseract",
        version="5.0.0-test",
        languages=("eng", "osd"),
        source="test",
        tessdata_prefix=None,
        requested_languages=("eng",),
        usable_languages=("eng",),
        missing_languages=(),
        probe_passed=True,
        probe_detail="synthetic test engine",
    )


def _pdf_bytes(*pages_text: str) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for text in pages_text:
        page = document.new_page()
        if text:
            page.insert_text((40, 80), text, fontsize=12)
    output = document.tobytes()
    document.close()
    return output


def test_scanned_pdf_uses_local_ocr_page_by_page(monkeypatch):
    from app import document_pipeline

    native = "This page already contains a full native text layer for indexing."
    pdf = _pdf_bytes(native, "")
    monkeypatch.setattr(document_pipeline, "ocr_engine", lambda **_kwargs: _fake_engine())
    monkeypatch.setattr(document_pipeline, "ocr_image", lambda *_args, **_kwargs: ("License number: R-12345", 91.5))

    outcome = documents.extract_document_pages(".pdf", pdf)
    assert outcome.detected_pages == 2
    assert outcome.method == "pdf_text+ocr"
    assert outcome.pages[0].method == "native"
    assert outcome.pages[1].method == "ocr"
    assert outcome.pages[1].page_number == 2
    assert outcome.pages[1].ocr_confidence == 91.5
    assert "R-12345" in outcome.pages[1].text
    assert len(outcome.usable_pages) == 2
    assert len(outcome.failed_pages) == 0


def test_image_ocr_and_actionable_missing_ocr_error(monkeypatch):
    from io import BytesIO

    from PIL import Image

    from app import document_pipeline

    buffer = BytesIO()
    Image.new("RGB", (480, 160), "white").save(buffer, format="PNG")
    png = buffer.getvalue()

    real_ocr_image = document_pipeline.ocr_image
    monkeypatch.setattr(document_pipeline, "ocr_engine", lambda **_kwargs: _fake_engine())
    monkeypatch.setattr(document_pipeline, "ocr_image", lambda *_args, **_kwargs: ("R-12345", 88.0))
    outcome = documents.extract_document_pages(".png", png)
    assert outcome.method == "image_ocr"
    assert outcome.pages[0].text == "R-12345"
    assert outcome.pages[0].page_number == 1

    monkeypatch.setattr(document_pipeline, "ocr_engine", lambda **_kwargs: None)
    monkeypatch.setattr(document_pipeline, "ocr_image", real_ocr_image)
    try:
        documents.extract_document_pages(".png", png)
    except document_pipeline.DocumentProcessingError as exc:
        assert exc.category == "ocr_unavailable"
        assert exc.retryable is True
        assert "OCR" in exc.message
    else:
        raise AssertionError("Missing OCR installation must return an actionable error.")


def test_office_pdf_writer_import_remains_available():
    # The synthetic PDF fixtures use PyMuPDF; keep pypdf's writer import exercised
    # so a broken dependency fails this module rather than a document upload.
    assert PdfWriter is not None


def test_unsupported_and_corrupt_files_return_clear_errors(client):
    unsupported = client.post(
        "/api/documents/upload",
        files={"file": ("sample.exe", b"bad", "application/octet-stream")},
    )
    assert unsupported.status_code == 415
    assert "Supported formats" in unsupported.json()["detail"]

    corrupt = client.post(
        "/api/documents/upload",
        files={"file": ("broken.pdf", b"%PDF-not-a-pdf", "application/pdf")},
    )
    assert corrupt.status_code == 400
    assert "PDF could not be read" in corrupt.json()["detail"]


def test_profile_seed_is_preserved_and_identity_answers_are_consistent(client):
    initial = client.get("/api/settings/profile").json()
    assert initial["full_name"] == "Rahil Wankhede"
    assert initial["preferred_name"] == "Rahil"

    updated = {**initial, "project_context": "Building the R Matrix command center."}
    profile_fields = {**updated}
    profile_fields.pop("updated_at", None)
    response = client.put("/api/settings/profile", json=profile_fields)
    assert response.status_code == 200
    assert client.get("/api/settings/profile").json()["project_context"] == updated["project_context"]

    who_am_i = client.post("/api/chat", json={"message": "Who am I?"})
    who_is_rahil = client.post("/api/chat", json={"message": "Who is Rahil?"})
    personal = client.post("/api/chat", json={"message": "What do you know about me?"})
    projects = client.post("/api/chat", json={"message": "What are my current projects?"})
    assert "You are Rahil." in who_am_i.json()["message"]["content"]
    assert "owner of this R Matrix Personal AI Command Center" in who_am_i.json()["message"]["content"]
    assert who_is_rahil.json()["message"]["content"] == "In this personal command center, Rahil refers to you, the owner."
    assert "Rahil Wankhede" in personal.json()["message"]["content"]
    assert updated["project_context"] in projects.json()["message"]["content"]


def test_task_deadlines_complete_delete_filters_and_reminder_cancellation(client):
    create = client.post(
        "/api/tasks",
        json={
            "title": "Renew license",
            "description": "Check the expiry date",
            "due_at": "2030-04-12T10:00",
            "reminder_preference": "1_day",
        },
    )
    assert create.status_code == 201, create.text
    task = create.json()
    assert task["due_at"] == "2030-04-12T04:30:00+00:00"
    with database.session() as connection:
        reminder = connection.execute(
            "SELECT scheduled_at, reminder_preference, status FROM task_reminders WHERE task_id = ?",
            (task["id"],),
        ).fetchone()
        assert reminder["scheduled_at"] == "2030-04-11T04:30:00+00:00"
        assert reminder["reminder_preference"] == "1_day"
        assert reminder["status"] == "pending"

    active = client.get("/api/tasks?status=pending").json()
    assert any(item["id"] == task["id"] for item in active)
    complete = client.post(f"/api/tasks/{task['id']}/complete")
    assert complete.status_code == 200
    assert complete.json()["status"] == "done"
    assert complete.json()["completed_at"]
    assert all(item["id"] != task["id"] for item in client.get("/api/tasks?status=pending").json())
    assert any(item["id"] == task["id"] for item in client.get("/api/tasks?status=done").json())
    with database.session() as connection:
        assert connection.execute(
            "SELECT status FROM task_reminders WHERE task_id = ?", (task["id"],)
        ).fetchone()["status"] == "cancelled"

    assert client.delete(f"/api/tasks/{task['id']}").status_code == 204
    assert all(item["id"] != task["id"] for item in client.get("/api/tasks").json())


def test_editing_deadline_replaces_pending_reminder_and_tasks_persist(client):
    task = client.post(
        "/api/tasks",
        json={"title": "Prepare report", "due_at": "2030-01-01T10:00", "reminder_preference": "at_due"},
    ).json()
    edited = client.put(
        f"/api/tasks/{task['id']}",
        json={
            "title": "Prepare final report",
            "description": "Review sources",
            "due_at": "2030-01-02T10:00",
            "reminder_preference": "15_minutes",
        },
    )
    assert edited.status_code == 200
    assert edited.json()["title"] == "Prepare final report"
    with database.session() as connection:
        jobs = connection.execute(
            "SELECT scheduled_at, reminder_preference, status FROM task_reminders WHERE task_id = ? ORDER BY created_at",
            (task["id"],),
        ).fetchall()
    assert [(job["reminder_preference"], job["status"]) for job in jobs] == [
        ("at_due", "cancelled"),
        ("15_minutes", "pending"),
    ]
    assert jobs[-1]["scheduled_at"] == "2030-01-02T04:15:00+00:00"
    assert any(item["id"] == task["id"] for item in client.get("/api/tasks").json())


def test_reminder_worker_sends_once_and_records_attempt(client, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "sender@example.test")
    with database.session() as connection:
        database.set_reminder_recipient(connection, "recipient@example.test")
    due = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    task = client.post(
        "/api/tasks",
        json={"title": "Past due task", "due_at": due, "reminder_preference": "at_due"},
    ).json()
    sent = []
    monkeypatch.setattr(reminders, "_send_email", lambda *args: sent.append(args))

    asyncio.run(reminders.process_due_reminders())
    asyncio.run(reminders.process_due_reminders())
    assert len(sent) == 1
    with database.session() as connection:
        row = connection.execute(
            "SELECT status, attempt_count, delivered_at FROM task_reminders WHERE task_id = ?",
            (task["id"],),
        ).fetchone()
        assert row["status"] == "sent"
        assert row["attempt_count"] == 1
        assert row["delivered_at"]


def test_reminder_failures_retry_and_interrupted_sends_are_not_duplicated(client, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "sender@example.test")
    with database.session() as connection:
        database.set_reminder_recipient(connection, "recipient@example.test")
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    task = client.post(
        "/api/tasks",
        json={"title": "Retry task", "due_at": due, "reminder_preference": "at_due"},
    ).json()

    def fail_send(*_args):
        raise OSError("Temporary SMTP error")

    monkeypatch.setattr(reminders, "_send_email", fail_send)
    asyncio.run(reminders.process_due_reminders())
    with database.session() as connection:
        row = connection.execute(
            "SELECT status, attempt_count, next_attempt_at, last_error FROM task_reminders WHERE task_id = ?",
            (task["id"],),
        ).fetchone()
        assert row["status"] == "pending"
        assert row["attempt_count"] == 1
        assert row["next_attempt_at"]
        assert "retry scheduled" in row["last_error"]
        connection.execute("UPDATE task_reminders SET status = 'sending' WHERE task_id = ?", (task["id"],))
        connection.commit()
    reminders._recover_interrupted_jobs()
    with database.session() as connection:
        row = connection.execute(
            "SELECT status, last_error FROM task_reminders WHERE task_id = ?", (task["id"],)
        ).fetchone()
    assert row["status"] == "failed"
    assert "not retried" in row["last_error"]


def test_agent_enable_controls_persist_and_gate_specialist_execution(client):
    catalog = client.get("/api/agents").json()
    document_agent = next(item for item in catalog["agents"] if item["id"] == "document_intelligence")
    assert document_agent["enabled"] is True
    response = client.put(
        "/api/agents/document_intelligence/enabled",
        json={"enabled": False},
    )
    assert response.status_code == 200
    assert client.get("/api/agents").json()["agents"][1]["enabled"] is False

    answer = client.post("/api/chat", json={"message": "What is written in my license?"})
    assert answer.status_code == 200
    assert "disabled in Settings" in answer.json()["message"]["content"]
    assert answer.json()["message"]["execution"]["status"] == "failed"

    assert client.put(
        "/api/agents/document_intelligence/enabled",
        json={"enabled": True},
    ).status_code == 200


def test_research_api_returns_configuration_error_without_calling_external_provider(client, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    status = client.get("/api/research/status")
    assert status.status_code == 200
    assert status.json()["configured"] is False
    result = client.post("/api/research", json={"query": "local test query"})
    assert result.status_code == 200
    assert result.json()["results"] == []
    assert result.json()["errors"][0]["code"] == "provider_not_configured"


def test_project_page_api_searches_only_authorized_configured_root(client, tmp_path, monkeypatch):
    project_file = tmp_path / "README.md"
    project_file.write_text("R Matrix supports a private document index.", encoding="utf-8")
    monkeypatch.setattr(settings, "authorized_project_dirs_raw", str(tmp_path))

    projects = client.get("/api/projects").json()["projects"]
    assert len(projects) == 1
    project_id = projects[0]["id"]
    result = client.post(
        f"/api/projects/{project_id}/search",
        json={"query": "private document index"},
    )
    assert result.status_code == 200
    assert result.json()
    assert result.json()[0]["path"] == "README.md"
    assert "private document index" in result.json()[0]["snippet"]


def test_reminder_settings_hide_credentials_and_mock_test_email(client, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_from", "sender@example.test")
    monkeypatch.setattr(settings, "smtp_username", "smtp-user-secret")
    monkeypatch.setattr(settings, "smtp_password", "smtp-password-secret")
    saved = client.put(
        "/api/settings/reminders",
        json={"recipient_email": "recipient@example.test"},
    )
    assert saved.status_code == 200
    assert saved.json()["smtp_configured"] is True
    assert "smtp-user-secret" not in saved.text
    assert "smtp-password-secret" not in saved.text

    calls = []

    async def fake_test_email(recipient):
        calls.append(recipient)

    monkeypatch.setattr(reminders, "send_test_email", fake_test_email)
    result = client.post("/api/settings/reminders/test", json={})
    assert result.status_code == 200
    assert calls == ["recipient@example.test"]


def test_invalid_smtp_port_is_rejected():
    from app.config import Settings

    try:
        Settings(smtp_port=70000)
    except ValueError as exc:
        assert "SMTP_PORT" in str(exc)
    else:
        raise AssertionError("Invalid SMTP port must be rejected.")
