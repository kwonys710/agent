@echo off
REM DailyReels Targeting Agent 실행 스크립트 (Windows)
REM 사용법:
REM   run_targeting.bat                       기본 실행(config.yaml 기준, Dry Run)
REM   run_targeting.bat --import my.csv       후보 파일 지정
REM   run_targeting.bat --stats               DB 현황만 확인
REM   run_targeting.bat --dashboard           로컬 Dashboard 실행

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

"%PYTHON%" -c "import sys; sys.exit(0)" > nul 2>&1
if errorlevel 1 (
    echo [오류] Python을 찾을 수 없습니다. Python 3.11 이상을 설치한 뒤 다시 실행하세요.
    pause
    exit /b 1
)

"%PYTHON%" -c "import yaml" > nul 2>&1
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
"%PYTHON%" -m targeting_agent.main %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" echo [종료 코드] %EXITCODE%
pause
exit /b %EXITCODE%
