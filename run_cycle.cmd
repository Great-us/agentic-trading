@echo off
REM Entry point for Windows Task Scheduler. Kept path-relative (%~dp0) so the
REM project can be moved without editing the registered task.
REM
REM run.py writes its own daily log under logs\; this file only catches output
REM that never reaches the logger - startup crashes, missing interpreter, etc.
REM
REM This file MUST keep CRLF line endings; cmd.exe misparses LF-only batch files.
REM %* lets the same wrapper be smoke-tested with --dry-run --skip-llm without
REM touching the account; Task Scheduler passes no arguments.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
echo ===== task fired %DATE% %TIME% ===== >> "logs\scheduler-task.log"
".venv\Scripts\python.exe" -m agentic_trading.run %* >> "logs\scheduler-task.log" 2>&1
echo ===== task exited with code %ERRORLEVEL% ===== >> "logs\scheduler-task.log"
