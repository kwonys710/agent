@echo off
REM 작업 스케줄러 등록 도우미. 인자 없이 실행하면 등록 내용만 보여준다(Dry Run).
REM   setup_scheduler.bat              19:00 기준 Dry Run
REM   setup_scheduler.bat 07:30        07:30 기준 Dry Run
REM   setup_scheduler.bat 07:30 install 실제 등록
REM   setup_scheduler.bat uninstall    등록 해제

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "TIME_ARG=%~1"
set "MODE=%~2"
if "%TIME_ARG%"=="" set "TIME_ARG=19:00"

if /I "%TIME_ARG%"=="uninstall" (
    powershell -ExecutionPolicy Bypass -File "targeting_agent\scripts\install_scheduler.ps1" -Uninstall
    pause
    exit /b %ERRORLEVEL%
)

if /I "%MODE%"=="install" (
    powershell -ExecutionPolicy Bypass -File "targeting_agent\scripts\install_scheduler.ps1" -Time "%TIME_ARG%" -Install
) else (
    powershell -ExecutionPolicy Bypass -File "targeting_agent\scripts\install_scheduler.ps1" -Time "%TIME_ARG%"
)
pause
exit /b %ERRORLEVEL%
