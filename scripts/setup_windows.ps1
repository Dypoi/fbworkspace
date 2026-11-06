# ============================================================
# FB Workspace - setup awal di Windows
# Jalankan di PowerShell, dari folder proyek:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot
Write-Host "Folder proyek: $ProjectRoot" -ForegroundColor Cyan

# 1) Cek Python
$pythonCmd = $null
foreach ($candidate in @("py -3.12", "py -3.11", "py -3.10", "python")) {
    try {
        $version = & cmd /c "$candidate --version" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match "Python 3\.(1[0-9])") {
            $pythonCmd = $candidate
            Write-Host "Python ditemukan: $version ($candidate)" -ForegroundColor Green
            break
        }
    } catch { }
}
if (-not $pythonCmd) {
    Write-Host "Python 3.10+ tidak ditemukan. Pasang dari https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "Saat memasang, centang 'Add python.exe to PATH'." -ForegroundColor Yellow
    exit 1
}

# 2) Virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "Membuat virtual environment .venv ..." -ForegroundColor Cyan
    & cmd /c "$pythonCmd -m venv .venv"
} else {
    Write-Host ".venv sudah ada, dipakai ulang." -ForegroundColor Green
}

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Gagal membuat .venv. Periksa izin folder dan coba lagi." -ForegroundColor Red
    exit 1
}

# 3) Dependensi
Write-Host "Memasang dependensi Python ..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "Instalasi dependensi gagal." -ForegroundColor Red
    exit 1
}

# 4) Browser Chromium untuk Playwright (opsional tapi disarankan untuk POC)
Write-Host "Mengunduh Chromium untuk Playwright (sekitar 150 MB) ..." -ForegroundColor Cyan
& $venvPython -m playwright install chromium

# 5) .env
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env dibuat dari .env.example (tanpa rahasia)." -ForegroundColor Green
} else {
    Write-Host ".env sudah ada, tidak ditimpa." -ForegroundColor Green
}

# 6) Folder data
New-Item -ItemType Directory -Force -Path "data", "logs" | Out-Null

Write-Host ""
Write-Host "Setup selesai." -ForegroundColor Green
Write-Host "Langkah berikutnya: .\scripts\run_windows.ps1" -ForegroundColor Cyan
Write-Host "Untuk menguji pembacaan grup: .\scripts\poc_windows.ps1 -Url ""<tautan grup>""" -ForegroundColor Cyan
