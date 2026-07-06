@echo off
echo 🚀 ALBION MVP — Quick Start
echo ============================
if not exist ".venv" python -m venv .venv
call .venv\Scripts\activate
pip install -q -r requirements.txt
if not exist ".env" copy .env.example .env
echo ✅ Starting...
python -m src.main
pause
