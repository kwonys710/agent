"""LIVE 경로를 실제 Playwright + 실제 Chromium으로 검증한다 (Phase 18B).

Instagram에 접속하지 않는다. 로컬 DOM fixture(127.0.0.1)에 좋아요/댓글을 수행해
운영 selector와 확인 로직이 실제 브라우저에서 동작하는지 본다.
"""
from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from targeting_agent.actions.executors.browser_page import STOP_ACTION_BLOCKED
from targeting_agent.discovery.browser_models import SessionState

pytest.importorskip("playwright.sync_api", reason="playwright 미설치")

from targeting_agent.actions.executors.browser_page import PlaywrightActionPage  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "instagram_dom"


def _executable_path() -> str:
    candidate = Path("/opt/pw-browsers/chromium")
    return str(candidate) if candidate.exists() else ""


@pytest.fixture(scope="module")
def dom_server() -> Iterator[str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def page(dom_server: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[PlaywrightActionPage]:
    action_page = PlaywrightActionPage(
        profile_dir=tmp_path_factory.mktemp("action-profile"),
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


def test_홈에서_로그인_상태를_확인한다(page: PlaywrightActionPage):
    assert page.ensure_logged_in() is SessionState.LOGGED_IN


def test_실제_브라우저에서_좋아요가_눌리고_확인된다(page: PlaywrightActionPage, dom_server: str):
    page.open_media(f"{dom_server}/reel.html")

    assert page.already_liked() is False
    assert page.like() is True
    assert page.already_liked() is True  # 상태 변화로 성공을 확인한다


def test_실제_브라우저에서_댓글이_입력되고_게시된다(page: PlaywrightActionPage, dom_server: str):
    page.open_media(f"{dom_server}/reel.html")
    text = "저도 퇴근하고 카페 가고 싶네요"

    assert page.comment(text) is True

    posted = page._page.locator("#comments li").all_inner_texts()
    assert posted == [text]


def test_차단_문구가_보이면_중단_사유를_돌려준다(page: PlaywrightActionPage, dom_server: str):
    page.open_media(f"{dom_server}/reel_blocked.html")

    assert page.stop_reason() == STOP_ACTION_BLOCKED


def test_정상_화면에서는_중단_사유가_없다(page: PlaywrightActionPage, dom_server: str):
    page.open_media(f"{dom_server}/reel.html")

    assert page.stop_reason() is None
