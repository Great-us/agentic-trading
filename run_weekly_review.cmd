@echo off
REM Weekly AI trade review. Registered in Task Scheduler as AgenticTradingWeeklyReview,
REM fires Sat 08:30 local (after Friday's 16:15 ET close cycle has journaled the week).
REM Spawns a one-shot Kimi instance (k3-256k, effort high via KIMI_CODE_HOME) that runs
REM the read-only evaluate/report scripts, attributes P&L from the journal, writes
REM research\weekly-review-<date>.md, then exits.
REM
REM This file MUST keep CRLF line endings; cmd.exe misparses LF-only batch files.
REM Keep this file pure ASCII -- cmd.exe reads it in the system codepage.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
echo ===== weekly review fired %DATE% %TIME% ===== >> "logs\weekly-review.log"
set "KIMI_CODE_HOME=C:\Users\helow\.kimi-code-trading"
"C:\Users\helow\.kimi-code\bin\kimi.exe" -m kimi-code/k3-256k -p "Read the file config/weekly-review-prompt.md in the current working directory and follow it exactly. All boundaries in it are mandatory." >> "logs\weekly-review.log" 2>&1
echo ===== weekly review exited with code %ERRORLEVEL% ===== >> "logs\weekly-review.log"
