@echo off
REM DailyReels Targeting Agent — Canary Auto Resume (Phase 18E.1)
REM Canary를 이어서 진행합니다. 가능할 때만 실제 LIKE를 수행합니다.
REM   - Claude 한도가 비었으면 WAITING_AI_BUDGET 으로 조용히 끝납니다.
REM   - 자격 후보가 없으면 WAITING_ELIGIBLE 로 조용히 끝납니다(임계값을 낮추지 않습니다).
REM 상한은 코드에 박혀 있습니다: 총 6 / 하루 3 / 1회 2 / 최소 8시간 간격.
REM config.yaml은 바뀌지 않습니다 — LIVE 전환은 프로세스 메모리 안에서만 일어납니다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PYTHON%" -m targeting_agent.main --canary-resume %*
exit /b %ERRORLEVEL%
