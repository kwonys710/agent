@echo off
REM DailyReels Targeting Agent — COMMENT Live Canary (Phase 19M)
REM 실제 공개 댓글을 상한(기본 1건)만큼 달고, 게시 여부를 확인한다.
REM
REM 먼저 리허설을 권한다:  run_targeting_comment_canary.bat --rehearsal
REM 실제 수행 조건:
REM   1) config.yaml 의 canary.comment.enabled: true
REM   2) run_instagram_session.bat 으로 로그인되어 있어야 한다
REM   3) 활동 상태가 NORMAL 이어야 한다(경고/Challenge/차단이면 수행하지 않는다)
REM 확인 불가(UNKNOWN)면 즉시 중단하고 같은 댓글을 다시 보내지 않는다.

chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if /i "%~1"=="--rehearsal" (
    "%PYTHON%" -m targeting_agent.main --comment-canary-rehearsal
    exit /b %ERRORLEVEL%
)

"%PYTHON%" -m targeting_agent.main --comment-canary %*
echo.
echo [안내] 실제 댓글이 달렸다면 Instagram 앱에서 직접 확인하세요.
exit /b %ERRORLEVEL%
