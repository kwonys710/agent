@echo off
REM DailyReels Targeting Agent — Instagram Browser Discovery (Phase 18A)
REM 검색으로 Reel 후보를 찾아 저장하고, 기존 분석 파이프라인으로 넘긴다.
REM 브라우저는 "읽기"만 한다 — 좋아요/댓글/팔로우/DM/저장/공유를 하지 않는다.
REM 먼저 run_instagram_session.bat 으로 로그인해 두어야 한다.
REM 로그인 필요 / 본인 확인 / 이용 제한 경고가 감지되면 즉시 중단한다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

"%PYTHON%" -c "import playwright" > nul 2>&1
if errorlevel 1 (
    echo [오류] playwright가 설치되어 있지 않습니다. 먼저 run_instagram_session.bat 을 실행하세요.
    exit /b 2
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PYTHON%" -m targeting_agent.main --discover %*
echo.
echo [안내] 수집 결과는 Dashboard(run_targeting_dashboard.bat)에서 확인·승인하세요.
exit /b %ERRORLEVEL%
