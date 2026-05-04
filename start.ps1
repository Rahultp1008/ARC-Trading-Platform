# start.ps1 â€” ARC Trading Platform Startup Script

# Check Redis Windows Service
Write-Host "Checking Redis..." -ForegroundColor Cyan
$redis = Get-Service -Name "Redis" -ErrorAction SilentlyContinue
if ($redis.Status -eq "Running") {
    Write-Host "Redis is running on port 6379" -ForegroundColor Green
} else {
    Write-Host "Starting Redis..." -ForegroundColor Yellow
    Start-Service -Name "Redis" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Write-Host "Redis started" -ForegroundColor Green
}

# Start FastAPI server
Write-Host "Starting ARC Trading server..." -ForegroundColor Cyan
uvicorn app.main:app --reload --reload-exclude .venv
