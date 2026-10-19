@echo off
REM DailyReels Targeting Agent — Scheduled Run (Windows Task Scheduler에서 호출)
REM inbox 처리 → 신규 Candidate 분석 → Daily Summary HTML 생성 후 종료한다.
REM Instagram 좋아요/댓글은 실행하지 않는다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

"%PYTHON%" -c "import yaml, pydantic" > nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install -r "targeting_agent\requirements.txt" > nul 2>&1
    if errorlevel 1 (
        echo [오류] 패키지 설치에 실패했습니다.
        exit /b 2
    )
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%PYTHON%" -m targeting_agent.main --scheduled %*
exit /b %ERRORLEVEL%
