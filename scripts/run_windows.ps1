# ============================================================
# FB Workspace - menjalankan dashboard lokal
#   powershell -ExecutionPolicy Bypass -File .\scripts\run_windows.ps1
#
# Dashboard SELALU diikat ke 127.0.0.1 (hanya laptop ini).
# Jangan ubah ke 0.0.0.0 kecuali Anda paham risikonya.
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host ".venv belum ada. Jalankan dulu: .\scripts\setup_windows.ps1" -ForegroundColor Red
    exit 1
}

$port = 8501
if (Test-Path ".env") {
    $line = Select-String -Path ".env" -Pattern "^FBW_PORT=(\d+)" | Select-Object -First 1
    if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
}

Write-Host "Membuka dashboard di http://127.0.0.1:$port (hanya lokal)" -ForegroundColor Cyan
Write-Host "Tekan Ctrl+C di jendela ini untuk berhenti." -ForegroundColor Yellow

& $venvPython -m streamlit run app.py `
    --server.address 127.0.0.1 `
    --server.port $port `
    --server.headless false `
    --browser.gatherUsageStats false
