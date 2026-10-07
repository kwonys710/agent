@echo off
REM DailyReels Targeting Agent — Autopilot (Phase 18C)
REM Discovery → 분석 → Target Score → 댓글 후보 → Action 결정 → Executor 까지 한 번에 실행한다.
REM 기본값은 REVIEW 모드(autopilot.enabled=false) + browser_executor.mode=DRY_RUN 이므로
REM 실제 좋아요/댓글은 수행되지 않는다. 실제 수행은 운영자가 config에서 명시적으로 켜야 한다.
REM 로그인 필요 / 본인 확인 / 이용 제한 경고 / 작업 차단이 감지되면 즉시 중단한다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PYTHON%" -m targeting_agent.main --autopilot %*
echo.
echo [안내] 결과는 Dashboard(run_targeting_dashboard.bat)와 data/reports 요약에서 확인하세요.
exit /b %ERRORLEVEL%
