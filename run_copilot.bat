@echo off
REM Launches the Polymarket Research Copilot (Flask app + in-process flywheel scheduler).
REM Used by the "PolymarketCopilot" Windows Scheduled Task (ONLOGON) so the flywheel keeps
REM running across reboots. Run it directly to start the app in a console too.
cd /d C:\users\ianme\projects\PMRA
set PYTHONPATH=src
"C:\Users\ianme\AppData\Local\Programs\Python\Python312\python.exe" -m research.app
