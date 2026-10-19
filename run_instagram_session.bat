@echo off
REM DailyReels Targeting Agent — Instagram 로그인 세션 준비 (Phase 18A)
REM 브라우저를 열어 두면 운영자가 "직접" 로그인한다.
REM ID/PW는 묻지도, 저장하지도, 자동 입력하지도 않는다.
REM 2단계 인증 / 본인 확인은 화면에서 운영자가 직접 처리한다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

"%PYTHON%" -c "import playwright" > nul 2>&1
if errorlevel 1 (
    echo [안내] playwright를 설치합니다...
    "%PYTHON%" -m pip install playwright > nul 2>&1
    if errorlevel 1 (
        echo [오류] playwright 설치에 실패했습니다.
        exit /b 2
    )
    "%PYTHON%" -m playwright install chromium
    if errorlevel 1 (
        echo [오류] Chromium 설치에 실패했습니다.
        exit /b 2
    )
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PYTHON%" -m targeting_agent.scripts.init_instagram_session %*
exit /b %ERRORLEVEL%
