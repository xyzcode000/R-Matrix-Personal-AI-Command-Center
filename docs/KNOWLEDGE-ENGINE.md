# Personal Knowledge Engine

This guide describes the document and approved-memory features in the local development application. It does not imply that the application is ready for public access.

## Documents

### Upload and storage

The Documents page accepts PDF, scanned PDF, DOC/DOCX, TXT, Markdown, CSV, XLSX, PPTX, PNG, JPG/JPEG, WEBP, BMP, and TIFF (including multi-page TIFF) uploads. HEIC is not accepted because no verified decoder is installed. The backend checks the extension, declared content type, size, actual file structure/signature, and safe archive expansion limits. Files receive generated UUID storage names; the original filename is display metadata only. The default upload limit is 10 MiB and can be changed with `MAX_UPLOAD_BYTES`.

Uploaded files and the SQLite database are stored under `%LOCALAPPDATA%\\RMatrixCommandCenter` on Windows, outside the frontend's public directory. Set `R_MATRIX_DATA_DIR` before starting FastAPI to select another application data directory. Uploads are not placed in the repository.

PDF text is extracted page by page with PyMuPDF; pages with insufficient native text are rendered and read with local Tesseract OCR. A mixed PDF therefore uses native text where present and OCR only where needed, without duplicating page content. Raster images are processed with orientation correction, conservative upscaling, grayscale and contrast enhancement before OCR. DOCX extracts headings, paragraphs, and table rows; XLSX extracts worksheet names and row/column-labeled cell values; PPTX extracts slide text and table rows; CSV extracts row/column-labeled values. DOC files use Antiword when installed. Markdown headings and DOCX heading styles are retained as section metadata when possible. Encrypted or malformed files, invalid encodings, mismatched MIME types, invalid signatures, and unsupported extensions show an error instead of being indexed as ready.

The Python requirements install PyMuPDF, python-docx, openpyxl, python-pptx, Pillow, and pytesseract. OCR additionally requires the real Tesseract executable; `pytesseract` alone is only a wrapper. Run `scripts\setup-document-ocr-windows.ps1` once. The backend resolves OCR in this order: explicit `TESSERACT_CMD`, `PATH`, standard Windows installation folders, then other safe configured locations. Each candidate is validated by running `--version`, listing language data, and OCR-ing a generated test image. An engine that cannot pass the probe is reported as unavailable rather than treated as installed. Optional `TESSDATA_PREFIX` selects language data. English (`eng`) is mandatory; Hindi (`hin`) is used automatically when its traineddata file is installed and is reported as missing otherwise.

PDF processing is limited to 100 pages total (`DOCUMENT_MAX_PDF_PAGES`) and at most 10 OCR pages per document (`DOCUMENT_OCR_MAX_PAGES`); image OCR is limited to 40 megapixels. These bounds limit local CPU and memory use. Pages beyond the OCR limit are recorded as needing retry, never silently dropped.

Each stored document records its display name, original filename, description, category, MIME type, extension, size, detected page count, processed page count, failed page count, processing status, OCR status, extraction method, SHA-256 content hash, chunk count, upload time, last processing time, attempt count and last attempt time. Each page records its page number, native/OCR method, OCR confidence when available, extraction status, sanitized error category and indexing result. Identical file contents are rejected as duplicates. Deleting a document removes its stored file and the database record (including dependent pages, chunks and jobs); failures are returned rather than shown as successful deletion.

### Chunking and retrieval

Extracted text is split into deterministic character-bounded chunks with overlap. Defaults are 1,000 characters per chunk and 150 characters of overlap. Configure them with `DOCUMENT_CHUNK_SIZE` and `DOCUMENT_CHUNK_OVERLAP`; overlap must be smaller than the chunk size. Boundaries prefer a line break before a word boundary, so a printed field label and its value usually stay in one chunk. PDF page numbers and available section headings are stored with each chunk. Each document is also classified at index time from its name and readable page text (Aadhaar, PAN, marksheet, migration, driving licence) and the result is stored in `documents.document_type`; documents indexed before this field existed are backfilled at backend startup.

Search is local, lexical BM25-style retrieval over the stored chunks, not embeddings or a vector database. It combines document-name, description and category relevance with chunk scoring, field-label boosts (PAN, Aadhaar, date of birth, marks) and common Indian document aliases such as Aadhaar/Aadhar/UID, PAN/Permanent Account Number, marksheet/marks statement, migration/transfer certificate and driving licence/license. When the question names a document type, chunks from that type are boosted and chunks from a different classified type are demoted, not blindly excluded. Results are capped at two passages per document so one file cannot flood the evidence, and identical long passages repeated across pages are deduplicated. Each selected result also carries its document type, page number and relevance score, and its excerpt is expanded with the other chunks from the same page so neighbouring label/value text is present in the evidence. Documents that are failed, retry-required or still processing are excluded from retrieval; they are surfaced separately with an explanation instead.

Exact-value questions (PAN number, Aadhaar number, date of birth) are answered from deterministic extraction over the retrieved passages before any model is called, with the source document and page. PAN candidates must match five letters, four digits and one letter; Aadhaar candidates must contain twelve digits with valid grouping. A format match does not prove a government-issued identifier is valid, so the response asks the owner to verify against the original page. If candidates disagree, the answer lists them as ambiguous instead of choosing silently.

Before retrieval, a deterministic query-understanding layer normalizes informal spellings (Aadhaar/Aadhar/adhar/UIDAI, PAN card, dad/mom, DOB, maths, 12th) into canonical field and document terms. It tracks the subject of the last six conversation turns, so an elliptical follow-up such as "give me no from any" after an Aadhaar question is answered as an Aadhaar question instead of being treated as unrelated chat. If the same subject is asked again after an unresolved answer, the search widens: more passages, a lower relative relevance floor, more results per document, and direct page-content matching for the requested document type. Repeating a question therefore never reuses the identical retrieval path after an ambiguous or unreadable result.

Other document questions send a bounded set of matching passages to the selected local model as separate labelled evidence blocks (document, page, type, relevance). The system prompt identifies the owner as the authorized reader, forbids refusing a value from the owner's own documents on sensitivity grounds, asks for every readable field when a whole document is requested, and requires uncertain readings to be explained rather than suppressed. A source is returned only when it supports the answer: an answer that could not find the information returns an empty source list and the exact message "I couldn't find that information in your uploaded documents." If the relevant document exists but its extraction is incomplete, the assistant says it has not been able to read that document yet and links only to that document. Each chat message stores its own sources, so a later unanswered question never displays an earlier message's sources. The model is instructed to treat excerpts as untrusted data and cite only supplied sources; generated answers can still be wrong.

### Cloud consent

Document storage and retrieval are local. Cloud providers do not receive document text by default. In AI Chat, select the per-request document-context option to preview the matching excerpts first. The dashboard displays the filenames and excerpt text and waits for a separate confirmation before sending them. At send time, the backend revalidates the reviewed excerpt set and rejects it if the indexed results changed. Canceling the preview sends no document context. Without document consent, a cloud provider receives the user's message and eligible conversation history, but not the local excerpts. Local Ollama requests may use matching excerpts without cloud transmission. This feature does not send document contents to web-search providers.

The local dashboard does not have a global login gate. Remote Access has a separate owner sign-in for device enrollment and remote-control operations. Keep FastAPI bound to loopback and do not expose it to a network.

## Approved memory

Memory stores concise user-approved facts or preferences; it does not store or replace source documents. It is not authoritative when a source needs to be checked. Clear memory requests and selected user-authored preferences, project details, or technical decisions can create a pending proposal. AI Chat asks for explicit approval in the same conversation; a clear affirmative response approves, a negative response rejects, and no proposal is automatically approved. Ordinary conversation text is not saved as memory.

Sensitive and credential-like content is excluded from automatic proposals. Approved memories are stored in the local SQLite database and only a relevant subset is supplied to local Ollama chat in a separate factual context block. Ordinary cloud chat does not receive approved-memory context; relevant memories can accompany project context only when the existing per-request cloud project-context consent is enabled. The memory system does not currently expose category editing.

## Owner profile and chat identity

The additive `owner_profile` table is seeded with Rahil Wankhede / Rahil only when no profile exists. Editing Settings updates the existing row and does not reset customized values. Identity questions such as “Who am I?” and “Who is Rahil?” are answered locally from the profile. For other local model calls, the central Manager may include profile fields and relevant approved memories as factual context, never as instructions. Profile context is excluded from cloud model requests by default. “What do you know about me?” lists only populated profile fields and approved memories; it does not invent missing details.

## Task deadlines and reminders

Tasks remain in the existing `application_tasks` table. Due times are interpreted as Asia/Kolkata when no offset is supplied, normalized to UTC in SQLite, and displayed in Asia/Kolkata. Active tasks can be edited, completed, or deleted separately. Completion sets `completed_at` and cancels pending reminders; deleted tasks are hidden from active/completed views and also cancel reminders. Completed tasks remain in Completed History until explicitly deleted.

Reminder preferences are No reminder, At due time, 15 minutes before, 1 hour before, or 1 day before. The persistent `task_reminders` queue survives backend restarts, retries temporary SMTP failures with bounded exponential backoff, and records delivery outcomes. Change the recipient in Settings. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_USE_TLS` in `backend/.env`; credentials are never stored in SQLite or returned to the frontend. A Gmail example uses `SMTP_HOST=smtp.gmail.com`, port 587, STARTTLS, and a Google App Password (with 2-Step Verification enabled), not the account password.

The local scheduler requires the backend and computer to be running. When the machine is asleep or the backend is stopped, delivery cannot happen at the exact deadline; eligible pending jobs are processed after restart. A job interrupted while being sent is recorded as delivery-unknown and is not automatically resent, to avoid duplicate email. This cannot guarantee exactly-once delivery across SMTP/network failures.

## OCR architecture and startup sequence

Document processing is implemented once, in `backend/app/document_pipeline.py` (OCR resolution, page extraction, health, consistency) and `backend/app/documents.py` (persistence, jobs, retry, retrieval). There is no second OCR, RAG or chat pipeline.

First-time setup on Windows:

1. Create the backend virtual environment and install `backend/requirements.txt`.
2. Run `powershell -ExecutionPolicy Bypass -File scripts\setup-document-ocr-windows.ps1` once. It detects an existing engine, installs Tesseract through winget when missing, verifies the executable and language data, and runs a synthetic OCR self-test. Use `-WriteEnv` if you want it to record `TESSERACT_CMD` in `backend/.env`.
3. Run `powershell -ExecutionPolicy Bypass -File scripts\diagnose-document-pipeline.ps1` to confirm the engine, languages, libraries and writable storage.
4. Start the backend from `backend`: `.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.
5. Start the frontend from `frontend`: `npm run dev`.
6. Open Documents and confirm the processing health panel says **Document processing ready**.

Both scripts are safe to run repeatedly. If the backend was already running before OCR was installed or an environment variable was changed, restart it. Text-based PDFs and office documents continue to work even when OCR is unavailable; scanned pages are reported as needing retry rather than failing silently. On startup the backend prints a readiness line such as `Document processing ready: Tesseract 5.4.0 (eng); optional language data missing: hin.` so a missing engine or language is visible in the console without opening the UI.

English OCR is verified at runtime. Hindi support is enabled automatically when `hin.traineddata` exists in Tesseract's `tessdata` folder (or in `TESSDATA_PREFIX`); it is reported as an optional missing language, not as a processing failure, and instructions for adding it are in the setup script output and README. Exact PAN/Aadhaar/DOB answers read the full stored page, include the page number, and are marked for visual verification when OCR confidence is low or more than one candidate appears. When a field label exists but no valid value could be read, the assistant says the value must be verified on that page instead of guessing or denying the document exists.

## Data changes and local operation

The application uses SQLite, not PostgreSQL, for its current local MVP. New document extraction metadata, task deadline fields, owner profile, reminder queue, and recipient settings are added idempotently when the existing database initializes; existing documents, chats, memories, tasks, and projects are not deleted or recreated. No separate migration command is required.

Use the existing Windows setup and launch steps in the repository [README](../README.md). After dependency installation, start FastAPI from `backend` and Next.js from `frontend`; keep both on their configured local addresses. The backend extraction dependencies are installed through `backend/requirements.txt`. Tesseract OCR is a separate local system installation; Antiword is an optional local executable for legacy DOC files.

Run backend tests from the repository root:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest .\backend\tests -q
```

Run frontend validation from `frontend`:

```powershell
npm run typecheck
npm run build
```

## Answer states, field evidence and provenance

Document questions are answered in layers. The document type is identified from the library metadata and the readable page text, then complete page text (not a truncated search snippet) is passed to `backend/app/document_fields.py`. That layer extracts labelled fields and, for marksheets, board names and subject rows. It returns one explicit state:

- **confirmed** — one reliably supported value, with the exact document and page.
- **ambiguous** — multiple plausible values or a value whose OCR quality is too low. No candidate is selected as the answer; the page references are shown for visual verification.
- **unreadable** — the label exists but no trustworthy value was read.
- **not_found** — the document was processed and the field is absent or not explicitly printed.
- **incomplete** — matching pages have not been fully processed, so absence cannot be claimed.
- **user_correction** — the owner supplied a value in conversation; it is labelled as user-provided, never as OCR text.

PAN, Aadhaar and date values use label context and structural checks (PAN shape; Aadhaar twelve digits plus Verhoeff as an additional signal, never proof of authenticity). Subject marks require the named subject row; a percentage is calculated only from a maximum printed in that row. Board names require an explicit board/authority label — the system does not infer a board from an acronym or disclaimer sentence. Low-confidence pages with identifier labels get one bounded higher-DPI OCR recovery pass during processing; a retry result is kept only when it improves confidence and does not lose most of the page text.

Citations are deduplicated per document/page, the answer state is attached to the message execution and to each source card, and conflicting names preserve every candidate with its page instead of choosing one. Two name readings that differ only by likely OCR substitutions (edit distance of at most two after folding 0/O, 1/I/l, 5/S, 8/B, Q/G, U/V, Y/W and trailing initials) are explained as possible OCR variants of one value rather than as two different people; genuinely different names remain separately listed. A user correction is kept only in conversation context (not OCR data, not permanent memory) and is identified as user-provided when the owner later asks what the document says.

PAN and Aadhaar values are never written to application logs: a logging filter replaces PAN-shaped and twelve-digit Aadhaar-shaped values with `PAN:REDACTED` and `AADHAAR:REDACTED`. This masking applies to logs only; the authenticated owner's chat response is not masked. API authentication, session cookies, CSRF checks and `REQUIRE_API_AUTH` are unchanged.

## Processing statuses, retry and recovery

Document statuses are `pending`, `validating`, `processing`, `text_extracting`, `ocr_processing`, `indexing`, `indexed` (Ready), `partial`, `failed` and `retry_required`. A document becomes Ready only when it produced at least one searchable chunk and every detected page was processed. `partial` means some pages are usable and indexed while others need retry; the UI shows detected, processed and failed page counts. `retry_required` means a missing or temporary dependency (for example OCR) prevented reading, and the original file is intact. A scanned document with zero chunks is never shown as Ready.

Each attempt is recorded in `document_processing_jobs` with its stage, page progress, attempt number, timestamps and sanitized error category. A per-document lock prevents two workers from processing the same document at once, OCR concurrency is bounded, and progress is persisted so a refresh shows the current stage. On backend startup, any job left `running` is marked interrupted and its document becomes `retry_required` instead of staying stuck forever. Retry re-reads the stored original and replaces pages/chunks transactionally, so repeating it never duplicates content. If a retry fails for a temporary dependency on a document that already had valid chunks, the previous verified index is preserved; a deterministic corruption failure clears the stale index rather than leaving it searchable.

Two distinct actions exist:

- **Retry processing** re-runs validation, extraction, OCR, chunking and indexing from the stored original. Use it when extraction or OCR failed, when a dependency is now installed, or when pages were partial.
- **Rebuild index** re-generates chunks from already-verified extracted pages only. Use it when extraction is valid and the search index needs repair; it never re-runs OCR and refuses when no usable pages exist.

The Documents page shows a compact processing health panel with OCR engine version, languages, PDF/image readiness, storage readiness, Ready/Partial/Retry/Failed counts, active jobs and the last successful processing time. `GET /api/documents/health` exposes the same non-sensitive information to the authenticated owner, and `GET /api/documents/diagnostics` performs read-only consistency checks: Ready-without-chunks, missing original files, missing pages, duplicate page records, count mismatches, interrupted jobs and stale processing statuses. Neither endpoint returns document text, identity values, secrets or storage paths.

## Backup and recovery

Before any manual repair, back up `%LOCALAPPDATA%\\RMatrixCommandCenter` (both SQLite databases and the `documents` folder) and verify that the copied files open. Keep `DOCUMENT_ENCRYPTION_KEY` separately from the backup: without it, encrypted originals cannot be decrypted, and the application never regenerates a key automatically. Restoring is: stop the backend, restore the database files and document folder from the verified backup, restore the same key in `backend/.env`, then start the backend and run the diagnostics script.

## Troubleshooting and limitations

- **OCR is unavailable:** run `scripts\setup-document-ocr-windows.ps1`, then `scripts\diagnose-document-pipeline.ps1`. Restart the backend after changing environment variables; restarting the UI does not change the backend process environment.
- **An existing failed document:** open Documents and choose **Retry processing**; do not delete and re-upload it. The original file is retained.
- **A PDF OCR job exceeds its limit:** split it into smaller files (at most 10 pages requiring OCR per document); text-based PDF pages do not count toward this OCR-page limit.
- **A DOC/DOCX/XLSX/PPTX is rejected:** verify the actual format, not only its extension, and keep it within the upload/archive expansion limits. Legacy DOC needs Antiword.
- **Text extraction fails:** TXT and Markdown must be UTF-8. Re-save the file as UTF-8 and retry.
- **Search finds no passage:** lexical retrieval can still miss unusual paraphrases. Try specific words used in the document, improve the document's display name/description, or check the Diagnostics panel for a missing page.
- **A document is marked partial:** open it and read which pages need retry. Retry processing runs extraction again only for the stored original; previously processed pages are replaced, not duplicated.
- **Encryption status:** original files use AES-256-GCM when `DOCUMENT_ENCRYPTION_KEY` is configured. Extracted page text and chunks live inside the local SQLite database; database-level encryption is not implemented, so protect the Windows account and the data directory. The application never claims database contents are encrypted when they are not.
- **Cloud preview is empty:** no matching excerpts were found. Continuing without excerpts shares only the question and eligible history; it does not make document evidence available to the model.
- **A reminder is pending but not delivered:** check the SMTP configuration status, recipient email, backend process, and Settings delivery counts. Email cannot be delivered while the local backend or machine is offline.

Semantic embeddings, multi-user authorization, public deployment protections, and PostgreSQL support are outside this milestone. The local workspace APIs do not require the separate Remote Access sign-in. Remote access uses the owner session, but local SQLite remains a single-user store and the application is not suitable for public deployment.
