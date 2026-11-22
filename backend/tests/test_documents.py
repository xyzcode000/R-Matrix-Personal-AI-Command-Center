from io import BytesIO

from docx import Document as WordDocument

from app import agents, database
from app.documents import _chunk_text


def _docx_bytes() -> bytes:
    document = WordDocument()
    document.add_heading("Retention policy", level=1)
    document.add_paragraph("The retention period is seven years.")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _profile_document_text() -> str:
    return """
# Personal Profile
Rahil Wankhede is a software developer based in Durg, Chhattisgarh, India. He is studying software engineering and building AI products.

# Technical Skills
Rahil is strongest in Python, FastAPI, Next.js, TypeScript, SQL, and machine learning. He is learning system design, MLOps, and prompt engineering.

# Career Direction
His preferred career direction is to become an AI product engineer who builds useful tools and works in full-stack product teams.

# R Matrix Personal AI Command Center
R Matrix Personal AI Command Center is a local AI workspace for personal document intelligence and project analysis. It uses FastAPI, Next.js, SQLite, Ollama, and cloud-model adapters when consent is enabled.

# Known Limitations
Known limitations include limited local retrieval quality, privacy and consent controls, and the fact that uploaded documents are treated as untrusted source text.

# Event Note
The DEALOS hackathon was hosted in Bhilai, Chhattisgarh, and the local community participated in a demo showcase.
""".strip()


def test_docx_upload_indexes_heading_and_metadata(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 100_000)
    response = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "policy.docx",
                _docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["extension"] == ".docx"
    assert document["status"] == "indexed"
    assert document["content_hash"]
    assert document["extracted_text_available"] is True
    assert document["chunk_count"] == 1
    assert document["last_processed_at"]

    results = client.post(
        "/api/documents/search",
        json={"query": "retention seven years"},
    ).json()
    assert len(results) == 1
    assert results[0]["section_heading"] == "Retention policy"
    assert results[0]["relevance_score"] > 0


def test_duplicate_upload_reindex_and_delete_remove_indexed_content(client):
    content = b"Unique local document about a blue heron."
    first = client.post(
        "/api/documents/upload",
        files={"file": ("notes.txt", content, "text/plain")},
    )
    assert first.status_code == 201
    document_id = first.json()["id"]

    duplicate = client.post(
        "/api/documents/upload",
        files={"file": ("copy.txt", content, "text/plain")},
    )
    assert duplicate.status_code == 409

    reindexed = client.post(f"/api/documents/{document_id}/reindex")
    assert reindexed.status_code == 200
    assert reindexed.json()["chunk_count"] == 1
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0] == 1

    stored_path = next((database.settings.documents_dir).glob(f"{document_id}.txt"))
    assert stored_path.exists()
    deleted = client.delete(f"/api/documents/{document_id}")
    assert deleted.status_code == 204
    assert not stored_path.exists()
    assert client.get("/api/documents").json() == []
    assert client.post("/api/documents/search", json={"query": "heron"}).json() == []


def test_upload_content_type_must_match_extension(client):
    response = client.post(
        "/api/documents/upload",
        files={"file": ("notes.txt", b"private text", "application/pdf")},
    )
    assert response.status_code == 415


def test_empty_upload_and_failed_reindex_are_reported_without_stale_chunks(client):
    empty = client.post(
        "/api/documents/upload",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert empty.status_code == 400

    uploaded = client.post(
        "/api/documents/upload",
        files={"file": ("notes.txt", b"Original indexed content.", "text/plain")},
    )
    document_id = uploaded.json()["id"]
    stored_path = database.settings.documents_dir / f"{document_id}.txt"
    stored_path.write_bytes(b"\xff")
    failed = client.post(f"/api/documents/{document_id}/reindex")
    assert failed.status_code == 400
    assert failed.json()["detail"] == "Text files must use UTF-8 encoding."
    listed = client.get("/api/documents").json()[0]
    assert listed["status"] == "failed"
    assert listed["processing_error"] == failed.json()["detail"]
    assert listed["chunk_count"] == 0
    assert client.post("/api/documents/search", json={"query": "Original"}).json() == []


def test_chunking_is_deterministic_bounded_and_overlapping(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "document_chunk_size", 100)
    monkeypatch.setattr(settings, "document_chunk_overlap", 20)
    text = "word " * 60
    first = _chunk_text(text, 3, "Section")
    second = _chunk_text(text, 3, "Section")
    assert first == second
    assert len(first) > 1
    assert all(len(chunk) <= 100 for _, _, chunk in first)
    assert all(page == 3 and heading == "Section" for page, heading, _ in first)
    assert all(chunk.strip() for _, _, chunk in first)


def test_personal_profile_queries_rank_profile_before_event_details(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 250_000)
    upload = client.post(
        "/api/documents/upload",
        files={"file": ("profile.md", _profile_document_text().encode("utf-8"), "text/markdown")},
    )
    assert upload.status_code == 201, upload.text

    personal = client.post("/api/documents/search", json={"query": "Who is Rahil?"}).json()
    assert personal
    assert personal[0]["section_heading"] == "Personal Profile"
    assert "Durg" in personal[0]["snippet"]
    assert "DEALOS" not in personal[0]["snippet"]

    about_me = client.post("/api/documents/search", json={"query": "Tell me about myself"}).json()
    assert about_me
    assert about_me[0]["section_heading"] == "Personal Profile"

    technical = client.post(
        "/api/documents/search",
        json={"query": "What are my main technical skills and my career direction?"},
    ).json()
    assert technical
    assert technical[0]["section_heading"] in {"Technical Skills", "Career Direction"}
    assert any(item["section_heading"] == "Technical Skills" for item in technical)
    assert any(item["section_heading"] == "Career Direction" for item in technical)


def test_self_and_project_questions_are_routed_to_document_search():
    assert agents.route_intent("Who is Rahil?")[0] == "documents"
    assert agents.route_intent("Who is Rahil Wankhede?")[0] == "documents"
    assert agents.route_intent("Tell me about myself")[0] == "documents"
    assert agents.route_intent("What do you know about my education?")[0] == "documents"
    assert agents.route_intent("What are my main technical skills?")[0] == "documents"
    assert agents.route_intent("What is R Matrix?")[0] == "documents"


def test_college_question_retrieves_the_education_passage(client):
    text = """
# Education
Rahil Wankhede studies B.Tech in Computer Science and Engineering at SSTC, Bhilai.

# Event Note
The DEALOS hackathon was hosted in Bhilai and the local community attended.
""".strip()
    upload = client.post(
        "/api/documents/upload",
        files={"file": ("education.md", text.encode("utf-8"), "text/markdown")},
    )
    assert upload.status_code == 201, upload.text

    results = client.post(
        "/api/documents/search",
        json={"query": "Which college do I attend?"},
    ).json()
    assert results
    assert results[0]["section_heading"] == "Education"
    assert "SSTC" in results[0]["excerpt"]
    assert "SSTC" in results[0]["snippet"] or "Education" in results[0]["snippet"]


def test_unrelated_questions_return_no_document_evidence(client):
    client.post(
        "/api/documents/upload",
        files={"file": ("profile.md", _profile_document_text().encode("utf-8"), "text/markdown")},
    )

    results = client.post(
        "/api/documents/search",
        json={"query": "zeppelin hydrodynamics"},
    ).json()
    assert results == []


def test_search_can_be_limited_to_specific_documents(client):
    first = client.post(
        "/api/documents/upload",
        files={"file": ("orbit-a.md", b"# Orbit\nThe orbit review belongs to the alpha notebook.", "text/markdown")},
    )
    second = client.post(
        "/api/documents/upload",
        files={"file": ("orbit-b.md", b"# Orbit\nThe orbit review belongs to the beta notebook.", "text/markdown")},
    )
    assert first.status_code == 201 and second.status_code == 201

    results = client.post(
        "/api/documents/search",
        json={"query": "orbit review", "document_ids": [first.json()["id"]]},
    ).json()
    assert results
    assert {result["document_id"] for result in results} == {first.json()["id"]}


def test_delete_conversation_removes_messages_but_keeps_documents_and_memories(client):
    with database.session() as connection:
        first_id = database.create_conversation(connection, "First conversation")
        second_id = database.create_conversation(connection, "Second conversation")
        database.add_message(connection, first_id, "user", "Hello")
        database.add_message(connection, first_id, "assistant", "Hi there")
        database.add_message(connection, second_id, "user", "Second topic")
        database.create_memory_proposal(connection, "I prefer Python.", "python", "test")
        connection.execute(
            "INSERT INTO approved_memories (id, content, content_hash, provenance, source_conversation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "memory-1",
                "I prefer Python.",
                "python",
                "test",
                first_id,
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()

    missing = client.delete(f"/api/conversations/{first_id}")
    assert missing.status_code == 204, missing.text

    with database.session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations WHERE id = ?", (first_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (first_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversations WHERE id = ?", (second_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM approved_memories").fetchone()[0] == 1

    not_found = client.delete("/api/conversations/does-not-exist")
    assert not_found.status_code == 404

    response = client.get("/api/conversations")
    ids = {item["id"] for item in response.json()}
    assert second_id in ids
    assert first_id not in ids
