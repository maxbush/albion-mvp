# ALBION MVP — Quick Start for PowerShell
Write-Host "🚀 ALBION MVP — Quick Start" -ForegroundColor Cyan
Write-Host "============================" -ForegroundColor Cyan
Write-Host ""

# Check Python
try {
    $pyVersion = python --version
    Write-Host "✅ Python: $pyVersion"
} catch {
    Write-Host "❌ Python not found. Install Python 3.13+" -ForegroundColor Red
    exit 1
}

# Create venv if needed
if (-not (Test-Path ".venv")) {
    Write-Host "📦 Creating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
}

# Activate
.\.venv\Scripts\Activate.ps1

# Install deps
Write-Host "📥 Installing dependencies..." -ForegroundColor Yellow
pip install -q -r requirements.txt

# Check .env
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" -Destination ".env"
    Write-Host ""
    Write-Host "⚠️  Created .env from .env.example" -ForegroundColor Yellow
    Write-Host "✏️  Open .env and set your TELEGRAM_BOT_TOKEN" -ForegroundColor Yellow
    Write-Host "   Get a token from @BotFather: https://t.me/botfather" -ForegroundColor Yellow
    exit 1
}

# Check token
$envContent = Get-Content ".env" -Raw
if ($envContent -notmatch "TELEGRAM_BOT_TOKEN=.+" -or $envContent -match "TELEGRAM_BOT_TOKEN=your_token_here") {
    Write-Host "⚠️  TELEGRAM_BOT_TOKEN is not set in .env" -ForegroundColor Yellow
    Write-Host "   Get a token from @BotFather and set it in .env" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "✅ Starting bot in polling mode..." -ForegroundColor Green
Write-Host "   Press Ctrl+C to stop" -ForegroundColor Gray
Write-Host ""

python -m src.main

# Deactivate on exit
deactivate
