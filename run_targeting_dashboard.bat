@echo off
REM DailyReels Targeting Agent — 운영 Dashboard 실행 (Windows)
REM 브라우저에서 http://127.0.0.1:8501 을 연다.
REM 이 화면은 Instagram 동작을 실행하지 않고 Action Queue와 Feedback만 관리한다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

"%PYTHON%" -c "import yaml, pydantic" > nul 2>&1
if errorlevel 1 (
    echo [설치] 필요한 패키지를 설치합니다...
    "%PYTHON%" -m pip install -r "targeting_agent\requirements.txt"
    if errorlevel 1 (
        echo [오류] 패키지 설치에 실패했습니다.
        pause
        exit /b 1
    )
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
start "" http://127.0.0.1:8501
"%PYTHON%" -m targeting_agent.main --dashboard %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [종료 코드] %EXITCODE%
    pause
)
exit /b %EXITCODE%
