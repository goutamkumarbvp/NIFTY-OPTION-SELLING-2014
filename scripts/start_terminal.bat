@echo off
cd /d "%~dp0\.."
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist .env ( copy .env.paper .env & echo Created .env from .env.paper - add your broker credentials (NEO_*) and run again & pause & exit /b 1 )
python -m terminal
pause
