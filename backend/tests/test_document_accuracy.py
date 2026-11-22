"""Synthetic tests for structured document answers, provenance and citation safety."""

from __future__ import annotations

from app import agents, document_fields as fields, documents
from app.config import settings

FAKE_PAN = "ABCDE1234F"
FAKE_AADHAAR = "234567890123"


def make_page(text: str, *, page: int = 1, confidence: float | None = 90.0, document_name="Synthetic identity card"):
    return fields.Page(
        document_id="doc-1",
        document_name=document_name,
        page_number=page,
        text=text,
        confidence=confidence,
    )


def valid_aadhaar(prefix: str = "23456789012") -> str:
    for digit in "0123456789":
        candidate = prefix + digit
        if fields.verhoeff_valid(candidate):
            return candidate
    raise AssertionError("no valid synthetic checksum found")


def test_pan_summary_lists_readable_fields_and_flags_unreadable_number():
    pages = [
        make_page(
            "PAN: A8CDE12M4F\nName: SYNTHETIC PERSON\nFather's Name: SYNTHETIC PARENT\n"
            "Date of Birth: 01/01/2000",
            confidence=55.0,
        )
    ]
    result = fields.answer_document_question("What information is available in my PAN card?", pages)
    assert result is not None
    assert result["state"] == "ambiguous"
    assert "SYNTHETIC PERSON" in result["answer"]
    assert "SYNTHETIC PARENT" in result["answer"]
    assert "not clear enough" in result["answer"]
    assert len(result["citations"]) == 1


def test_confirmed_pan_requires_label_format_and_reliable_page():
    confirmed = fields.answer_document_question("What is my PAN number?", [make_page(f"PAN: {FAKE_PAN}")])
    assert confirmed and confirmed["state"] == "confirmed" and confirmed["value"] == FAKE_PAN

    low = fields.answer_document_question(
        "What is my PAN number?", [make_page(f"PAN: {FAKE_PAN}", confidence=40.0)]
    )
    assert low and low["state"] == "ambiguous"
    assert FAKE_PAN not in low["answer"]


def test_multiple_aadhaar_candidates_are_never_arbitrarily_chosen():
    pages = [
        make_page("Aadhaar: 2345 6789 0123", page=1),
        make_page("Aadhaar Number: 3456 7890 1234", page=2),
    ]
    result = fields.answer_document_question("What's my Aadhaar card number?", pages)
    assert result is not None
    assert result["state"] == "ambiguous"
    assert "2345 6789 0123" not in result["answer"]
    assert "3456 7890 1234" not in result["answer"]
    assert {item.page for item in result["citations"]} == {1, 2}


def test_same_aadhaar_repeated_on_front_and_back_is_one_candidate():
    number = valid_aadhaar()
    groups = f"{number[:4]} {number[4:8]} {number[8:]}"
    pages = [
        make_page(f"Aadhaar: {groups}", page=1),
        make_page(f"Aadhaar Number: {groups}", page=2),
    ]
    assert fields.verhoeff_valid(number)
    result = fields.answer_document_question("What's my Aadhaar number?", pages)
    assert result is not None
    assert result["state"] == "confirmed"
    assert number in result["value"].replace(" ", "")


def test_board_requires_an_explicit_name_not_a_disclaimer_abbreviation():
    explicit = fields.answer_document_question(
        "From which board did I pass my 12th?",
        [make_page("Board: CENTRAL BOARD OF SECONDARY EDUCATION\nMATHEMATICS 84 100")],
    )
    assert explicit is not None
    assert explicit["state"] == "confirmed"
    assert "CENTRAL BOARD OF SECONDARY EDUCATION" in explicit["answer"]

    disclaimer = fields.answer_document_question(
        "From which board did I pass my 12th?",
        [make_page("taking examination of any University or Board established by law.")],
    )
    assert disclaimer is not None
    assert disclaimer["state"] == "not_found"
    assert disclaimer["citations"] == []


def test_subject_marks_use_the_subject_row_and_supported_maximum():
    page = make_page("| 041 MATHEMATICS 100 | 84 00 00 84P | EIGHTY FOUR |", confidence=90.0)
    result = fields.answer_document_question("How much marks did I get in Maths?", [page])
    assert result is not None
    assert result["value"].startswith("84")
    assert "out of 100" in result["answer"]
    assert "84.0%" in result["answer"]


def test_subject_marks_in_words_do_not_invent_a_maximum():
    page = make_page(
        "| 999 MATHEMATICS 999 | 99 XX XX 99P | EIGHTY FOUR B",
        confidence=59.8,
    )
    result = fields.answer_document_question("How much did I score in Maths?", [page])
    assert result is not None
    assert result["value"] == "84"
    assert "maximum marks for that row were not clearly readable" in result["answer"]
    assert "base" not in result["answer"].casefold()


def test_conflicting_parent_names_stay_ambiguous_and_keep_provenance():
    pages = [
        make_page("Father's Name: SYNTHETIC PERSON", page=1),
        make_page("Father's Name: SYNTHETIC PARSON", page=2),
    ]
    result = fields.answer_document_question("What's my father's name?", pages)
    assert result is not None
    assert result["state"] == "ambiguous"
    assert "more than one possible" in result["answer"]
    assert "SYNTHETIC PERSON" in result["answer"]
    assert "SYNTHETIC PARSON" in result["answer"]
    assert {item.page for item in result["citations"]} == {1, 2}


def test_name_label_on_next_line_is_read_and_low_confidence_is_flagged():
    pages = [make_page("Father's Name\nSYNTHETIC PARENT", confidence=55.0)]
    result = fields.answer_document_question("What's my father's name?", pages)
    assert result is not None
    assert result["state"] == "ambiguous"
    assert "SYNTHETIC PARENT" in result["answer"]
    assert "isn't reliable" in result["answer"] or "check the original" in result["answer"]


def test_focused_ocr_retry_is_bounded_and_never_downgrades_text():
    from app import document_pipeline

    assert document_pipeline.should_refine_ocr("PAN: ABCDE1234F", 24.0)
    assert not document_pipeline.should_refine_ocr("PAN: ABCDE1234F", 80.0)
    assert not document_pipeline.should_refine_ocr("Plain page text", 20.0)
    assert document_pipeline.prefer_ocr_result("old text", 30.0, "a better longer text", 45.0)
    assert not document_pipeline.prefer_ocr_result("old text", 30.0, "a better longer text", 32.0)
    assert not document_pipeline.prefer_ocr_result("a complete page of text", 30.0, "tiny", 60.0)


def test_citation_deduplication_keeps_one_source_per_document_page():
    sources = [
        {"document_id": "doc-1", "page_number": 3, "filename": "a"},
        {"document_id": "doc-1", "page_number": 3, "filename": "a", "snippet": "duplicate"},
        {"document_id": "doc-1", "page_number": 4, "filename": "a"},
    ]
    deduped = documents.dedupe_sources(sources)
    assert len(deduped) == 2
    assert [source["page_number"] for source in deduped] == [3, 4]


def test_generic_query_terms_do_not_make_an_unsupported_field_supported():
    sources = [{"document_id": "doc-1", "page_number": 1, "snippet": "Candidate number and card details"}]
    assert documents.sources_support_query("What is my passport number?", sources) is False
    assert documents.sources_support_query("What is my card number?", sources) is True


def test_user_correction_provenance_is_detected_without_storing_memory():
    history = [
        {"role": "assistant", "content": "I couldn't read your father's name clearly."},
        {"role": "user", "content": "It is Synthetic Parent"},
    ]
    assert agents._recent_document_correction(history) == ("father's name", "Synthetic Parent")
    assert agents._looks_like_document_correction("It is Synthetic Parent")
    assert agents._asks_what_document_says("What does the document actually say?")


def test_memory_claim_is_rewritten_when_nothing_was_saved():
    content = "Got it. I will remember this for future conversations."
    guarded = agents._guard_memory_claims(content, memory_stored=False)
    assert "I will remember" not in guarded
    assert "haven't saved" in guarded
    assert agents._guard_memory_claims(content, memory_stored=True) == content


def test_internal_retrieval_terms_are_removed_from_user_facing_text():
    content = (
        "According to the untrusted reference passage, BM25 returned this with a chunk score 0.82. "
        "Internal source validation failed."
    )
    cleaned = agents._naturalize_answer(content)
    for term in ("untrusted", "BM25", "chunk score", "Internal source validation failed"):
        assert term not in cleaned


def test_full_question_sequence_states_provenance_and_source_isolation(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 2_000_000)
    content = (
        f"PAN: {FAKE_PAN}\n"
        "Aadhaar: 2345 6789 0123\n"
        "Aadhaar Number: 3456 7890 1234\n"
        "Board: CENTRAL BOARD OF SECONDARY EDUCATION\n"
        "MATHEMATICS 84 100\n"
        "Father's Name: SYNTHETIC PARENT\n"
        "Mother's Name: SYNTHETIC MOTHER\n"
    ).encode("utf-8")
    upload = client.post(
        "/api/documents/upload",
        files={"file": ("synthetic-records.txt", content, "text/plain")},
        data={"display_name": "Synthetic 12th records", "category": "Education"},
    )
    assert upload.status_code == 201, upload.text

    conversation_id: str | None = None

    def ask(message: str) -> dict:
        nonlocal conversation_id
        payload: dict = {"message": message}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        conversation_id = result["conversation_id"]
        return result

    info = ask("What information is available in my PAN card?")
    assert info["execution"]["answer_state"] == "confirmed"
    assert FAKE_PAN in info["message"]["content"]

    aadhaar = ask("What's my Aadhaar card number?")
    assert aadhaar["execution"]["answer_state"] == "ambiguous"
    assert "2345 6789 0123" not in aadhaar["message"]["content"]
    assert "can't safely confirm" in aadhaar["message"]["content"]
    assert len(aadhaar["message"]["sources"]) == 1

    board = ask("From which board did I pass my 12th?")
    assert board["execution"]["answer_state"] == "confirmed"
    assert "CENTRAL BOARD OF SECONDARY EDUCATION" in board["message"]["content"]

    maths = ask("How much did I score in Maths?")
    assert maths["execution"]["answer_state"] == "confirmed"
    assert "84" in maths["message"]["content"] and "84.0%" in maths["message"]["content"]
    assert len(maths["message"]["sources"]) == 1

    father = ask("What's my father's name?")
    assert father["execution"]["answer_state"] == "confirmed"

    correction = ask("It is SYNTHETIC PARENT TWO")
    assert correction["execution"]["answer_state"] == "user_correction"
    assert "came from you" in correction["message"]["content"]
    assert "haven't saved" in correction["message"]["content"]
    assert correction["message"]["sources"] == []

    provenance = ask("What does the document actually say?")
    assert "came from you" in provenance["message"]["content"]
    assert "document" in provenance["message"]["content"].casefold()

    missing = ask("What is my passport number?")
    assert missing["message"]["content"] == "I couldn't find that information in your uploaded documents."
    assert missing["message"]["sources"] == []
    assert missing["execution"]["answer_state"] == "not_found"
    # Sources from earlier answers never appear on this message.
    assert not any(source.get("document_id") for source in missing["message"]["sources"])


def test_pan_info_chat_exposes_answer_state_and_single_citation(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 2_000_000)
    response = client.post(
        "/api/documents/upload",
        files={
            "file": (
                "synthetic-identity.txt",
                f"PAN: {FAKE_PAN}\nName: SYNTHETIC PERSON\n".encode("utf-8"),
                "text/plain",
            )
        },
        data={"display_name": "Synthetic PAN card", "category": "Identity"},
    )
    assert response.status_code == 201, response.text

    chat = client.post("/api/chat", json={"message": "What information is available in my PAN card?"})
    assert chat.status_code == 200, chat.text
    result = chat.json()
    message = result["message"]
    assert FAKE_PAN in message["content"]
    assert result["execution"]["answer_state"] == "confirmed"
    document_sources = [source for source in message["sources"] if source.get("document_id")]
    assert len(document_sources) == len({(source["document_id"], source["page_number"]) for source in document_sources})
    assert document_sources[0]["answer_state"] == "confirmed"

    missing = client.post("/api/chat", json={"message": "What is my passport number?"})
    missing_result = missing.json()["message"]
    assert missing_result["content"] == "I couldn't find that information in your uploaded documents."
    assert missing_result["sources"] == []
