"""실제 Chromium으로 LIVE LIKE 경로를 검증한다 (Phase 18E.64).

**Instagram에 접속하지 않는다.** 로컬 HTML fixture를 127.0.0.1로 띄우고,
실제 Playwright + 실제 Chromium으로 운영 코드가 쓰는 selector와 확인 절차를
그대로 돌린다. 실제 LIKE를 누르기 전에 여기서 먼저 깨지는지 본다.

확인하는 것:
  - 좋아요 버튼을 찾아 실제로 누른다
  - 누른 뒤 상태가 '좋아요 취소'로 바뀐다
  - **새로고침한 뒤에도** 그 상태가 남아 있다
  - 남지 않으면 UNKNOWN_WRITE_STATE로 처리하고 다시 누르지 않는다
"""
from __future__ import annotations

import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from targeting_agent.canary.executor import (
    UNKNOWN_WRITE_STATE,
    CanaryViolation,
    LikeOnlyExecutor,
)
from targeting_agent.core.models import ActionStatus, ActionType, QueuedAction

pytest.importorskip("playwright.sync_api", reason="playwright 미설치")

from targeting_agent.actions.executors.browser import BrowserExecutor  # noqa: E402
from targeting_agent.actions.executors.browser_page import PlaywrightActionPage  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "instagram_dom"


def _executable_path() -> str:
    candidate = os.environ.get("TARGETING_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
    return candidate if Path(candidate).exists() else ""


@pytest.fixture(scope="module")
def dom_server() -> Iterator[str]:
    """로컬 fixture 전용 http 서버(127.0.0.1, 외부로 나가지 않는다)."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def page(dom_server: str, tmp_path: Path) -> Iterator[PlaywrightActionPage]:
    action_page = PlaywrightActionPage(
        profile_dir=tmp_path / "profile",
        headless=True,
        timeout_ms=8000,
        selector_timeout_ms=1500,
        executable_path=_executable_path() or None,
        home_url=f"{dom_server}/",
    )
    try:
        action_page.start()
    except Exception as exc:  # pragma: no cover - Chromium 미설치 환경
        pytest.skip(f"Chromium을 실행할 수 없습니다: {exc}")
    try:
        yield action_page
    finally:
        action_page.close()


def _executor(page: PlaywrightActionPage) -> LikeOnlyExecutor:
    """실제 LIVE 조합(mode=LIVE + dry_run=false)으로 Executor를 만든다."""
    inner = BrowserExecutor(dry_run=False, config=None, conn=None, page=page, mode="LIVE")
    assert inner.live is True, "두 스위치가 모두 풀려야 LIVE다"
    return LikeOnlyExecutor(inner)


def _action(url: str) -> QueuedAction:
    return QueuedAction(
        action_id=1,
        media_pk=1,
        media_id="AAA111",
        permalink=url,
        username="office_daily_kim",
        creator_id=1,
        action_type=ActionType.LIKE,
        target_score=90.0,
        priority=90,
    )


def test_실제_chromium에서_좋아요를_누르고_새로고침까지_확인한다(
    page: PlaywrightActionPage, dom_server: str
):
    executor = _executor(page)

    result = executor.execute_like(_action(f"{dom_server}/reel_live.html"))

    assert result.status is ActionStatus.SUCCESS
    assert result.dry_run is False
    assert "verified" in result.detail
    assert executor.confirmed == 1
    # 새로고침한 화면에서도 '좋아요 취소' 상태가 보인다.
    assert page.already_liked() is True


def test_상태가_남지_않으면_unknown_write_state로_처리한다(
    page: PlaywrightActionPage, dom_server: str
):
    """click은 됐지만 새로고침하면 사라지는 화면 — 눌렸는지 알 수 없다."""
    executor = _executor(page)

    result = executor.execute_like(_action(f"{dom_server}/reel_live_notpersist.html"))

    assert result.status is ActionStatus.FAILED
    assert result.error == UNKNOWN_WRITE_STATE
    assert executor.unknown_write == 1
    assert executor.confirmed == 0


def test_이미_좋아요한_게시물은_다시_누르지_않는다(
    page: PlaywrightActionPage, dom_server: str
):
    """다시 누르면 좋아요가 취소된다 — 가장 피해야 할 동작이다."""
    executor = _executor(page)
    url = f"{dom_server}/reel_live.html"
    first = executor.execute_like(_action(url))
    assert first.status is ActionStatus.SUCCESS

    second = executor.execute_like(_action(url))

    assert second.status is ActionStatus.SKIPPED
    assert second.detail == "already_liked"
    assert page.already_liked() is True  # 여전히 좋아요 상태다(취소되지 않았다)


def test_실제_chromium에서도_comment는_막힌다(page: PlaywrightActionPage, dom_server: str):
    executor = _executor(page)
    action = _action(f"{dom_server}/reel_live.html")
    action.action_type = ActionType.COMMENT
    action.comment_text = "퇴근길 좋네요"

    with pytest.raises(CanaryViolation):
        executor.execute_comment(action)

    assert executor.comment_attempts == 1
