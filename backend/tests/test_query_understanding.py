"""Regression tests for query understanding, OCR variants and log safety."""

from __future__ import annotations

import logging

from app import agents, document_fields as fields, documents, log_redaction
from app import query_understanding as understanding
from app.config import settings

FAKE_PAN = "ABCDE1234F"


def valid_aadhaar(prefix: str = "23456789012") -> str:
    for digit in "0123456789":
        candidate = prefix + digit
        if fields.verhoeff_valid(candidate):
            return candidate
    raise AssertionError("no valid synthetic checksum found")


def test_informal_field_phrasings_normalize_to_one_canonical_intent():
    variants = [
        "What is my Aadhaar number?",
        "my aadhar card no",
        "give me my adhar card no",
        "aadhar number",
        "UIDAI number",
    ]
    for message in variants:
        intent = understanding.inspect_query(message)
        assert "aadhaar_number" in intent.fields, message
        assert "aadhaar" in intent.document_types, message

    assert "pan_number" in understanding.inspect_query("my pan card no").fields
    assert "father_name" in understanding.inspect_query("dad name").fields
    assert "mother_name" in understanding.inspect_query("mom's name").fields
    assert "date_of_birth" in understanding.inspect_query("my DOB").fields
    maths = understanding.inspect_query("maths marks")
    assert "subject_marks" in maths.fields
    assert understanding.subject_for_query("maths marks") == "mathematics"


def test_elliptical_follow_up_inherits_the_earlier_subject_and_widens():
    history = [
        {"role": "user", "content": "What is my Aadhaar number?"},
        {"role": "assistant", "content": "I found more than one possible Aadhaar number."},
    ]
    intent = understanding.resolve_follow_up("give me no from any", history)
    assert intent.inherited is True
    assert "aadhaar_number" in intent.fields
    assert "aadhaar" in intent.document_types
    assert intent.widen is True
    assert "aadhaar" in understanding.search_query(intent)


def test_follow_up_with_its_own_field_does_not_inherit_the_previous_subject():
    history = [{"role": "user", "content": "What is my Aadhaar number?"}]
    intent = understanding.resolve_follow_up("what about my father's name?", history)
    assert intent.inherited is False
    assert "father_name" in intent.fields
    assert "aadhaar_number" not in intent.fields


def test_repeat_detection_only_widens_after_an_unresolved_answer():
    unresolved = [
        {"role": "user", "content": "What is my PAN number?"},
        {"role": "assistant", "content": "The PAN field is not readable.", "execution": {"answer_state": "ambiguous"}},
    ]
    resolved = [
        {"role": "user", "content": "What is my PAN number?"},
        {"role": "assistant", "content": "Your PAN number is ...", "execution": {"answer_state": "confirmed"}},
    ]
    repeated = understanding.inspect_query("give me my pan number again")
    assert understanding.is_repeat_query(repeated, unresolved) is True
    assert understanding.is_repeat_query(repeated, resolved) is False


def test_conflicting_names_that_are_ocr_variants_are_explained_not_split():
    pages = [
        fields.Page("doc-1", "Synthetic identity", 1, "Father's Name: KARAN SHARMA", confidence=90.0),
        fields.Page("doc-1", "Synthetic identity", 2, "Father's Name: KARAN SHARNA", confidence=88.0),
    ]
    result = fields.answer_document_question("What's my father's name?", pages)
    assert result is not None
    assert result["state"] == "ambiguous"
    assert result.get("ocr_variants") is True
    assert "OCR variants" in result["answer"]
    assert "KARAN SHARMA" in result["answer"] and "KARAN SHARNA" in result["answer"]


def test_genuinely_different_names_are_not_collapsed_into_variants():
    pages = [
        fields.Page("doc-1", "Synthetic identity", 1, "Father's Name: KARAN SHARMA", confidence=90.0),
        fields.Page("doc-1", "Synthetic identity", 2, "Father's Name: VIKRAM PATEL", confidence=90.0),
    ]
    result = fields.answer_document_question("What's my father's name?", pages)
    assert result is not None
    assert result["state"] == "ambiguous"
    assert not result.get("ocr_variants")
    assert "KARAN SHARMA" in result["answer"] and "VIKRAM PATEL" in result["answer"]


def test_ocr_variant_detection_is_conservative():
    assert understanding.names_are_ocr_variants("SASHANK WANKHEDE Q", "SASHANK WANKHEDE")
    assert understanding.names_are_ocr_variants("KARAN SHARMA", "KARAN SHARNA")
    assert not understanding.names_are_ocr_variants("KARAN SHARMA", "VIKRAM PATEL")
    assert not understanding.names_are_ocr_variants("RAM", "RAMESH KUMAR")


def test_log_redaction_masks_government_ids_only_for_logs():
    text = "Aadhaar 2345 6789 0123 and PAN ABCDE1234F were read."
    masked = log_redaction.redact_government_ids(text)
    assert "2345 6789 0123" not in masked
    assert "ABCDE1234F" not in masked
    assert "AADHAAR:REDACTED" in masked and "PAN:REDACTED" in masked
    assert log_redaction.redact_government_ids("No identifiers here.") == "No identifiers here."

    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "value %s", ("ABCDE1234F",), None
    )
    assert log_redaction.GovernmentIdRedactionFilter().filter(record) is True
    assert "ABCDE1234F" not in record.getMessage()


def test_chunk_boundaries_keep_field_labels_with_their_values(monkeypatch):
    monkeypatch.setattr(settings, "document_chunk_size", 40)
    monkeypatch.setattr(settings, "document_chunk_overlap", 5)
    text = "Father's Name: SYNTHETIC PARENT\nMother's Name: SYNTHETIC MOTHER\n"
    chunks = [chunk for _, _, chunk in documents._chunk_text(text, 1, None)]
    assert any("Father's Name: SYNTHETIC PARENT" in chunk for chunk in chunks)
    assert any("Mother's Name: SYNTHETIC MOTHER" in chunk for chunk in chunks)


def test_document_type_is_stored_and_returned_with_sources(client):
    response = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "aadhaar-synthetic.txt",
                b"Aadhaar: 2345 6789 0123\nName: SYNTHETIC PERSON\n",
                "text/plain",
            )
        },
        data={"display_name": "Aadhaar card", "category": "Identity"},
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert "aadhaar" in document["document_types"]

    results = client.post("/api/documents/search", json={"query": "aadhaar number"}).json()
    assert results
    assert "aadhaar" in (results[0]["document_type"] or "")


def test_chat_follow_up_answers_the_inherited_aadhaar_subject(client):
    number = valid_aadhaar()
    grouped = f"{number[:4]} {number[4:8]} {number[8:]}"
    response = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "aadhaar-synthetic.txt",
                f"Aadhaar: {grouped}\nName: SYNTHETIC PERSON\n".encode("utf-8"),
                "text/plain",
            )
        },
        data={"display_name": "Aadhaar card", "category": "Identity"},
    )
    assert response.status_code == 201, response.text

    first = client.post("/api/chat", json={"message": "What's my Aadhaar number?"})
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]
    assert first.json()["execution"]["answer_state"] == "confirmed"
    assert grouped in first.json()["message"]["content"]

    follow_up = client.post(
        "/api/chat",
        json={"message": "give me no from any", "conversation_id": conversation_id},
    )
    assert follow_up.status_code == 200, follow_up.text
    result = follow_up.json()
    assert result["execution"]["agent_id"] == "document_intelligence"
    assert result["execution"]["answer_state"] == "confirmed"
    assert grouped in result["message"]["content"]

    other_field = client.post(
        "/api/chat",
        json={"message": "what about my father's name?", "conversation_id": conversation_id},
    )
    assert other_field.status_code == 200, other_field.text
    assert other_field.json()["execution"]["agent_id"] == "document_intelligence"
    assert other_field.json()["execution"]["answer_state"] in {
        "confirmed",
        "ambiguous",
        "unreadable",
        "not_found",
        "incomplete",
    }


def test_evidence_blocks_carry_document_type_and_source_label():
    block = agents._document_evidence_block(
        2,
        {
            "filename": "synthetic.pdf",
            "page_number": 3,
            "document_type": "aadhaar",
            "relevance_score": 0.9,
            "excerpt": "Aadhaar: 2345 6789 0123",
        },
    )
    assert "--- EVIDENCE BLOCK 2 ---" in block
    assert "[Source 2: synthetic.pdf, page 3]" in block
    assert "Document type: aadhaar" in block
    assert "Relevance: high" in block
