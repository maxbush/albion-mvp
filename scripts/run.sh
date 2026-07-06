#!/bin/bash
# ALBION MVP — Quick Start
set -e
echo "🚀 ALBION MVP — Quick Start"
echo "============================"
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 required."
    exit 1
fi
if [ ! -d ".venv" ]; then
    echo "📦 Creating venv..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "📥 Installing..."
pip install -q -r requirements.txt
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚠️  Edit .env and set TELEGRAM_BOT_TOKEN"
    exit 1
fi
grep -q "TELEGRAM_BOT_TOKEN=" .env || { echo "⚠️  Set TELEGRAM_BOT_TOKEN in .env"; exit 1; }
echo "" && echo "✅ Starting..." && echo ""
python -m src.main
