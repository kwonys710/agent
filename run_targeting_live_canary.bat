@echo off
REM DailyReels Targeting Agent — Limited Live Canary (Phase 18E)
REM ** 실제 Instagram에 좋아요를 누릅니다. ** 댓글/팔로우/DM은 하지 않습니다.
REM 상한은 코드에 박혀 있습니다: 총 6건 / 하루 3건 / 1회 2건 / 48시간 / 최소 8시간 간격.
REM config.yaml은 바뀌지 않습니다 — LIVE 전환은 이 프로세스 메모리 안에서만 일어납니다.
REM 먼저 run_instagram_session.bat 으로 로그인해 두어야 합니다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PYTHON%" -m targeting_agent.main --live-canary %*
exit /b %ERRORLEVEL%
