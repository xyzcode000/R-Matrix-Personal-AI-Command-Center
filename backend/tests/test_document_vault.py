"""Document Intelligence Vault: metadata, retrieval, viewer file access, bulk delete, encryption."""

import base64

import pytest

from app import crypto, database


def _upload(client, *, filename="licence.txt", content=b"DRIVING LICENCE\nLicence number: TEST-123456789\nExpiry date: 2045-01-09", name="TEST DRIVING LICENCE", description="Synthetic test licence for automated tests.", category="Identity", content_type="text/plain"):
    return client.post(
        "/api/documents/upload",
        files={"file": (filename, content, content_type)},
        data={"display_name": name, "description": description, "category": category},
    )


def test_upload_persists_owner_metadata_and_library_filters(client):
    uploaded = _upload(client)
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()
    assert document["display_name"] == "TEST DRIVING LICENCE"
    assert document["description"].startswith("Synthetic test licence")
    assert document["category"] == "Identity"
    assert document["original_name"] == "licence.txt"
    assert document["encryption"] == "aes-256-gcm"

    library = client.get("/api/documents/library").json()
    assert library["total"] == 1
    assert library["facets"]["categories"][0] == {"value": "Identity", "count": 1}

    assert client.get("/api/documents/library", params={"q": "driving"}).json()["total"] == 1
    assert client.get("/api/documents/library", params={"q": "licence number"}).json()["total"] == 1
    assert client.get("/api/documents/library", params={"q": "unrelated"}).json()["total"] == 0
    assert client.get("/api/documents/library", params={"category": "Identity"}).json()["total"] == 1
    assert client.get("/api/documents/library", params={"category": "Financial"}).json()["total"] == 0
    assert client.get("/api/documents/library", params={"extension": "png"}).json()["total"] == 0
    assert client.get("/api/documents/library", params={"status": "indexed"}).json()["total"] == 1


def test_document_name_outranks_unrelated_documents(client):
    _upload(client, filename="college.txt", content=b"College ID card\nRoll number: C-77\nAddress: Campus Road", name="College ID", category="Education")
    licence = _upload(client).json()

    results = client.post("/api/documents/search", json={"query": "what is my driving licence number"}).json()
    assert results, "the licence document should be retrievable by its name"
    assert results[0]["document_id"] == licence["id"]
    assert results[0]["document_name"] == "TEST DRIVING LICENCE"
    assert results[0]["page_number"] is None  # single-section plain-text document
    assert "TEST-123456789" in results[0]["excerpt"]


def test_original_file_is_served_authenticated_and_supports_ranges(client, anonymous_client):
    document = _upload(client).json()

    anonymous = anonymous_client.get(f"/api/documents/{document['id']}/file")
    assert anonymous.status_code == 401

    response = client.get(f"/api/documents/{document['id']}/file")
    assert response.status_code == 200
    assert response.content.startswith(b"DRIVING LICENCE")
    assert response.headers["content-type"].startswith("text/plain")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"

    partial = client.get(f"/api/documents/{document['id']}/file", headers={"Range": "bytes=0-6"})
    assert partial.status_code == 206
    assert partial.content == b"DRIVING"
    assert partial.headers["content-range"] == f"bytes 0-6/{len(response.content)}"

    invalid = client.get(f"/api/documents/{document['id']}/file", headers={"Range": "bytes=99999-100000"})
    assert invalid.status_code == 416


def test_metadata_update_changes_library_entry(client):
    document = _upload(client).json()
    updated = client.patch(
        f"/api/documents/{document['id']}",
        json={"display_name": "Driving Licence", "description": "Personal licence.", "category": "identity"},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Driving Licence"
    assert updated.json()["category"] == "Identity"
    assert updated.json()["original_name"] == "licence.txt"

    invalid = client.patch(f"/api/documents/{document['id']}", json={"display_name": " ", "description": "", "category": "x"})
    assert invalid.status_code in {200, 422}


def test_bulk_delete_removes_only_selected_documents(client):
    keep = _upload(client, filename="keep.txt", content=b"Keep this passage", name="Keep me").json()
    drop_one = _upload(client, filename="drop1.txt", content=b"Drop this passage", name="Drop one").json()
    drop_two = _upload(client, filename="drop2.txt", content=b"Drop that passage", name="Drop two").json()

    response = client.post(
        "/api/documents/bulk-delete",
        json={"document_ids": [drop_one["id"], drop_two["id"], "00000000-0000-0000-0000-000000000000"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert sorted(payload["deleted"]) == sorted([drop_one["id"], drop_two["id"]])
    assert len(payload["failed"]) == 1

    remaining = client.get("/api/documents").json()
    assert [item["id"] for item in remaining] == [keep["id"]]
    assert not any(database.settings.documents_dir.glob(f"{drop_one['id']}*"))
    remaining_ids = {item["document_id"] for item in client.post("/api/documents/search", json={"query": "Drop that passage"}).json()}
    assert drop_one["id"] not in remaining_ids
    assert drop_two["id"] not in remaining_ids


def test_page_endpoint_returns_extracted_pages(client):
    document = _upload(client).json()
    pages = client.get(f"/api/documents/{document['id']}/pages").json()
    assert pages["document"]["id"] == document["id"]
    assert "TEST-123456789" in pages["pages"][0]["content"]

    # Plain-text documents are stored as one unnumbered section.
    assert pages["pages"][0]["page_number"] is None
    assert client.get(f"/api/documents/{document['id']}/pages", params={"page": 9}).json()["pages"] == []


def test_original_files_are_encrypted_at_rest(client):
    document = _upload(client).json()
    stored = database.settings.documents_dir / f"{document['id']}.txt"
    raw = stored.read_bytes()
    assert crypto.is_encrypted(raw)
    assert b"TEST-123456789" not in raw
    assert crypto.decrypt_bytes(raw).startswith(b"DRIVING LICENCE")

    # The raw database copy of extracted text is intentionally still queryable; the
    # encryption guarantee covers the stored originals served to the browser.
    with database.session() as connection:
        content = connection.execute(
            "SELECT content FROM document_pages WHERE document_id = ?", (document["id"],)
        ).fetchone()["content"]
    assert "TEST-123456789" in content


def test_encrypted_document_fails_closed_without_matching_key(client, monkeypatch):
    document = _upload(client).json()
    monkeypatch.setenv("DOCUMENT_ENCRYPTION_KEY", base64.b64encode(b"a-different-32-byte-test-key-xx!").decode())
    blocked = client.get(f"/api/documents/{document['id']}/file")
    assert blocked.status_code == 503
    assert "could not be decrypted" in blocked.json()["detail"]

    monkeypatch.setenv("DOCUMENT_ENCRYPTION_KEY", "")
    missing = client.get(f"/api/documents/{document['id']}/file")
    assert missing.status_code == 503
    assert "not configured" in missing.json()["detail"]


def test_vault_endpoints_require_owner_session(anonymous_client):
    assert anonymous_client.get("/api/documents/library").status_code == 401
    assert anonymous_client.post("/api/documents/bulk-delete", json={"document_ids": ["x"]}).status_code == 401
    assert anonymous_client.patch("/api/documents/anything", json={"display_name": "x"}).status_code == 401
    assert anonymous_client.get("/api/documents/anything/file").status_code == 401
    assert anonymous_client.get("/api/documents/anything/pages").status_code == 401
