@echo off
REM Login / manual start for the read-only dashboard. Binds 127.0.0.1:8600 only.
REM This file MUST keep CRLF line endings; cmd.exe misparses LF-only batch files.
REM Task Scheduler should invoke pythonw.exe directly (see config/scheduled-tasks/sched-dashboard.xml)
REM so a cmd.exe window does not stay open for the life of the server.
cd /d "%~dp0"
".venv\Scripts\pythonw.exe" -m agentic_trading.dashboard.api --host 127.0.0.1 --port 8600