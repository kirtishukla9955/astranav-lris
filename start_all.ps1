# start_all.ps1
# AstraNav-LRIS — Start both services + open frontend
# Run from project root: .\start_all.ps1

$ROOT = $PSScriptRoot

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AstraNav-LRIS — Team Aura++ Launcher  " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Detection Service (port 8001) ──────────────────────────────────────────
Write-Host "[1/3] Starting Detection Service on port 8001..." -ForegroundColor Yellow
$detectJob = Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "cd '$ROOT'; `$env:PYTHONPATH='$ROOT'; python -m uvicorn detection.main:app --port 8001 --reload"
) -PassThru

Start-Sleep -Seconds 3

# ── Routing Backend (port 8000) ────────────────────────────────────────────
Write-Host "[2/3] Starting Routing Backend on port 8000..." -ForegroundColor Yellow
$backendJob = Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "cd '$ROOT\backend'; `$env:MEMBER1_LIVE='1'; `$env:MEMBER1_BASE_URL='http://localhost:8001'; `$env:ANTHROPIC_API_KEY='YOUR_API_KEY_HERE'; python -m uvicorn main:app --port 8000 --reload"
) -PassThru

Start-Sleep -Seconds 3

# ── Frontend ───────────────────────────────────────────────────────────────
Write-Host "[3/3] Opening frontend in browser..." -ForegroundColor Yellow
Start-Process "http://localhost:8000/api/regions"   # confirm backend alive
Start-Process "$ROOT\frontend\index.html"

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Services started:" -ForegroundColor Green
Write-Host "  Detection : http://localhost:8001/docs" -ForegroundColor Green
Write-Host "  Backend   : http://localhost:8000/docs" -ForegroundColor Green
Write-Host "  Frontend  : frontend/index.html" -ForegroundColor Green
Write-Host ""
Write-Host "  LIVE MODE: MEMBER1_LIVE=1 (real ice data)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C in each terminal to stop." -ForegroundColor Gray
