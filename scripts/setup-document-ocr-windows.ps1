# R Matrix Document OCR setup (Windows) — safe to run repeatedly.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup-document-ocr-windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup-document-ocr-windows.ps1 -WriteEnv
#
# Verifies the actual Tesseract executable (not just the Python wrapper), checks
# English language data, installs Tesseract through winget when it is missing,
# and runs the backend's synthetic OCR self-test. It never reads, prints or
# uploads an owner document.

[CmdletBinding()]
param(
    [switch]$WriteEnv,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot "backend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"
$envFile = Join-Path $backend ".env"

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "OK  $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "!!  $message" -ForegroundColor Yellow }
function Write-Fail($message) { Write-Host "XX  $message" -ForegroundColor Red }

Write-Step "Checking the backend Python environment"
if (-not (Test-Path $venvPython)) {
    Write-Fail "Backend virtual environment not found at backend\.venv."
    Write-Host "Create it first:  cd backend; python -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt"
    exit 1
}
$pythonVersion = & $venvPython --version 2>&1
Write-Ok $pythonVersion

Write-Step "Checking Python document libraries"
$libraryCheck = & $venvPython -c "import importlib.util as u; missing=[n for n in ('pytesseract','pymupdf','PIL','pypdf') if u.find_spec(n) is None]; print(','.join(missing))" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "The backend Python could not be queried."
    exit 1
}
if ($libraryCheck.Trim().Length -gt 0) {
    Write-Warn "Missing Python packages: $libraryCheck"
    Write-Host "Install them with:  cd backend; .\.venv\Scripts\python -m pip install -r requirements.txt"
} else {
    Write-Ok "pytesseract, PyMuPDF, Pillow and pypdf are installed"
}

function Resolve-Tesseract {
    $candidates = @()
    if ($env:TESSERACT_CMD) { $candidates += $env:TESSERACT_CMD }
    $onPath = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += @(
        (Join-Path $env:ProgramFiles "Tesseract-OCR\tesseract.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Tesseract-OCR\tesseract.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe"),
        (Join-Path $env:LOCALAPPDATA "Tesseract-OCR\tesseract.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

Write-Step "Locating the Tesseract OCR executable"
$tesseract = Resolve-Tesseract
if (-not $tesseract) {
    if ($SkipInstall) {
        Write-Fail "Tesseract is not installed and -SkipInstall was requested."
        Write-Host "Install it from https://github.com/UB-Mannheim/tesseract/wiki (official Windows build)."
        exit 1
    }
    Write-Warn "Tesseract is not installed. Trying winget (UB-Mannheim.TesseractOCR)."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Fail "winget is unavailable. Install Tesseract manually from the UB-Mannheim release page."
        exit 1
    }
    & winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "winget could not install Tesseract (exit $LASTEXITCODE). This often means administrator approval is required."
        Write-Host "Run this script from an elevated PowerShell window, or install Tesseract manually from"
        Write-Host "https://github.com/UB-Mannheim/tesseract/wiki and run this script again."
        exit 1
    }
    $tesseract = Resolve-Tesseract
}
if (-not $tesseract) {
    Write-Fail "Tesseract was installed but the executable could not be located."
    Write-Host "Set TESSERACT_CMD in backend\.env to the full path of tesseract.exe and run this script again."
    exit 1
}
Write-Ok "Executable: $tesseract"

Write-Step "Verifying the OCR engine version"
$versionLines = & $tesseract --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "The OCR executable failed to report its version."
    exit 1
}
Write-Ok ($versionLines | Select-Object -First 1)

Write-Step "Checking installed OCR language data"
$langLines = & $tesseract --list-langs 2>&1
$languages = @()
foreach ($line in $langLines) {
    $value = "$line".Trim()
    if ($value -and $value -notmatch "available languages" -and $value -notmatch "\s" -and $value -notmatch ":$") {
        $languages += $value
    }
}
if ($languages -notcontains "eng") {
    Write-Fail "English OCR data (eng.traineddata) is missing."
    Write-Host "Reinstall Tesseract and enable the English language data component."
    exit 1
}
Write-Ok ("Languages: " + ($languages -join ", "))
if ($languages -notcontains "hin") {
    Write-Warn "Hindi OCR data (hin.traineddata) is not installed."
    Write-Host "Hindi documents will still process with English only. To add Hindi:"
    Write-Host "  winget install --id UB-Mannheim.TesseractOCR -e --override '/S /D=C:\Program Files\Tesseract-OCR'"
    Write-Host "  then add Hindi under 'Additional language data' in the installer, or copy hin.traineddata"
    Write-Host "  from https://github.com/tesseract-ocr/tessdata_fast into the tessdata folder."
}

Write-Step "Running the backend synthetic OCR self-test (no owner documents are read)"
Push-Location $backend
try {
    $env:PYTHONPATH = $backend
    $selfTest = & $venvPython -m app.document_pipeline 2>&1
    $exit = $LASTEXITCODE
    $selfTest | Write-Host
    if ($exit -ne 0) {
        Write-Fail "The backend self-test failed. Review the message above."
        exit 1
    }
} finally {
    Pop-Location
}

if ($WriteEnv) {
    Write-Step "Writing TESSERACT_CMD to backend\.env (existing value is preserved)"
    $existing = if (Test-Path $envFile) { Get-Content $envFile -Raw } else { "" }
    if ($existing -match "(?m)^\s*TESSERACT_CMD\s*=") {
        Write-Ok "TESSERACT_CMD is already configured; nothing changed."
    } else {
        $prefix = if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) { "`r`n" } else { "" }
        Add-Content -Path $envFile -Value ($prefix + "TESSERACT_CMD=$tesseract") -Encoding utf8
        Write-Ok "TESSERACT_CMD written. Restart the backend to load it."
    }
}

Write-Host ""
Write-Ok "Local document processing is ready. Text PDFs, scanned PDFs and images can now be processed."
Write-Host "Restart the backend if it was already running:  cd backend; .\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
exit 0
