# ============================================================
# FB Workspace - POC pembacaan satu grup Facebook
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\poc_windows.ps1 `
#       -Url "https://www.facebook.com/groups/123456789/"
#
# Parameter opsional:
#   -MaxPosts 5 -MaxSeconds 90 -Headless -Yes
#   -Selector 'div[role="article"]'   (opsional; biarkan kosong agar strategi bawaan dipakai)
# ============================================================

param(
    [Parameter(Mandatory = $true)][string]$Url,
    [int]$MaxPosts = 5,
    [int]$MaxSeconds = 90,
    [string]$Selector = "",
    [switch]$Headless,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host ".venv belum ada. Jalankan dulu: .\scripts\setup_windows.ps1" -ForegroundColor Red
    exit 1
}

$arguments = @(
    "scripts\poc_facebook.py",
    "--url", $Url,
    "--max-posts", $MaxPosts,
    "--max-seconds", $MaxSeconds
)
# Selector hanya dikirim bila Anda benar-benar mengisinya. Tanpa ini,
# PowerShell 5.1 membuang tanda kutip sehingga selector bawaan berubah menjadi
# mode kustom (dan hanya komentar yang terbaca).
if (-not [string]::IsNullOrWhiteSpace($Selector)) {
    $arguments += @("--selector", $Selector)
}
if ($Headless) { $arguments += "--headless" }
if ($Yes) { $arguments += "--yes" }

& $venvPython @arguments
exit $LASTEXITCODE
