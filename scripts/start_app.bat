@echo off
REM Start the Polymarket Research Copilot Flask app.
REM Uses the full-path Python install + PYTHONPATH=src (mirrors `make run`).
REM No .venv / no `make` on this machine, so we invoke python directly.

cd /d C:\Users\ianme\projects\PMRA

REM Ensure the log directory exists (gitignored data/ dir).
if not exist data mkdir data

set PYTHONPATH=src
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m research.app >> data\app.log 2>&1
