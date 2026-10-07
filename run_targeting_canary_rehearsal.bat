@echo off
REM DailyReels Targeting Agent — Live Canary 리허설 (Phase 18E)
REM 전 구간을 DRY_RUN으로만 돌립니다. 실제 Instagram 쓰기는 0입니다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PYTHON%" -m targeting_agent.main --canary-rehearsal %*
exit /b %ERRORLEVEL%
