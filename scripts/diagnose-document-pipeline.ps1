# R Matrix document-pipeline diagnostics (Windows) — read-only and safe to repeat.
#
#   powershell -ExecutionPolicy Bypass -File scripts\diagnose-document-pipeline.ps1
#
# Reports Python, OCR engine/languages, PDF/image libraries, configuration flags,
# writable storage and document status counts. It never prints document text,
# identity values, encryption keys or environment secret values.

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot "backend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"

function Section($title) { Write-Host "`n--- $title ---" -ForegroundColor Cyan }

Section "Project"
Write-Host "Repository: $repoRoot"
if (-not (Test-Path $venvPython)) {
    Write-Host "XX backend\.venv\Scripts\python.exe is missing. Create the virtual environment first." -ForegroundColor Red
    exit 1
}
Write-Host ("Python: " + (& $venvPython --version 2>&1))

Section "Python packages"
$packageReport = & $venvPython -c @"
import importlib.util as u
for name in ('pytesseract','pymupdf','PIL','pypdf','fastapi','docx','openpyxl','pptx'):
    spec = u.find_spec(name)
    print(f'{name}: ' + ('installed' if spec else 'MISSING'))
"@ 2>&1
$packageReport | Write-Host

Section "Environment configuration (names only)"
$envPath = Join-Path $backend ".env"
if (Test-Path $envPath) {
    $names = Get-Content $envPath | Where-Object { $_ -match "^\s*[A-Z0-9_]+\s*=" } | ForEach-Object { ($_ -split "=", 2)[0].Trim() }
    foreach ($name in @("TESSERACT_CMD", "TESSDATA_PREFIX", "DOCUMENT_OCR_LANGUAGES",
                        "DOCUMENT_OCR_MAX_PAGES", "DOCUMENT_OCR_DPI", "R_MATRIX_DATA_DIR",
                        "DOCUMENT_ENCRYPTION_KEY")) {
        if ($names -contains $name) {
            if ($name -eq "DOCUMENT_ENCRYPTION_KEY") { Write-Host "$name: configured (value hidden)" }
            else { Write-Host "$name: configured" }
        } else {
            Write-Host "$name: not set (automatic discovery)"
        }
    }
} else {
    Write-Host "backend\.env is missing. Copy backend\.env.example and configure it."
}

Section "OCR engine and language data"
Push-Location $backend
try {
    $env:PYTHONPATH = $backend
    & $venvPython -m app.document_pipeline 2>&1 | Write-Host
} finally {
    Pop-Location
}

Section "Writable storage and document status"
$dataReport = & $venvPython -c @"
import json, os, sqlite3
from app.config import settings
result = {'data_dir': str(settings.data_dir), 'documents_dir': str(settings.documents_dir)}
try:
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    probe = settings.documents_dir / '.write-probe'
    probe.write_bytes(b'ok'); probe.unlink(missing_ok=True)
    result['documents_writable'] = True
except OSError:
    result['documents_writable'] = False
try:
    connection = sqlite3.connect(settings.database_path, timeout=5)
    connection.row_factory = sqlite3.Row
    rows = connection.execute('SELECT status, COUNT(*) AS n FROM documents GROUP BY status').fetchall()
    result['document_status'] = {row['status']: row['n'] for row in rows}
    stale = connection.execute(\"SELECT COUNT(*) FROM documents WHERE status = 'processing'\").fetchone()[0]
    result['interrupted_processing'] = stale
    inter = connection.execute(\"SELECT COUNT(*) FROM document_processing_jobs WHERE status IN ('running','interrupted')\").fetchone()[0]
    result['stale_jobs'] = inter
    missing = 0
    for row in connection.execute('SELECT storage_name FROM documents'):
        if not row['storage_name'] or not (settings.documents_dir / row['storage_name']).exists():
            missing += 1
    result['missing_original_files'] = missing
    connection.close()
except sqlite3.Error as exc:
    result['database_error'] = type(exc).__name__
print(json.dumps(result, indent=2))
"@ 2>&1
$dataReport | Write-Host

Write-Host "`nDiagnostics complete. The Documents page also shows these checks under Diagnostics." -ForegroundColor Green
exit 0
