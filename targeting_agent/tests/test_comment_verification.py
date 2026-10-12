"""댓글 게시 확인(Phase 19G)을 실제 Playwright + 실제 Chromium으로 검증한다.

Instagram에 접속하지 않는다. 로컬 DOM fixture(127.0.0.1)만 사용한다.
"입력란이 비었다"가 아니라 **목록에 보이고 reload 후에도 남아 있는지**를 본다.
"""
from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from targeting_agent.actions.executors.browser_page import (
    COMMENT_CONFIRMED,
    COMMENT_NOT_SENT,
    COMMENT_UNKNOWN_STATE,
    normalize_comment_text,
)

pytest.importorskip("playwright.sync_api", reason="playwright 미설치")

from targeting_agent.actions.executors.browser_page import PlaywrightActionPage  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "instagram_dom"
TEXT = "저도 퇴근하고 카페 가고 싶네요"


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
        profile_dir=tmp_path_factory.mktemp("comment-profile"),
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


def _open_clean(action_page: PlaywrightActionPage, url: str) -> None:
    """탭 하나를 테스트 전체가 공유하므로 fixture 상태를 비우고 시작한다."""
    action_page.open_media(url)
    action_page._page.evaluate("sessionStorage.clear()")
    action_page.open_media(url)  # 비운 상태로 다시 렌더링


def _submit_clicks(action_page: PlaywrightActionPage) -> int:
    """fixture가 sessionStorage에 세어 둔 게시 버튼 클릭 수(reload에도 남는다)."""
    return int(action_page._page.evaluate("Number(sessionStorage.getItem('reel_submit_clicks') || '0')"))


def test_댓글이_목록에_보이면_확인된다(page: PlaywrightActionPage, dom_server: str):
    _open_clean(page, f"{dom_server}/reel.html")

    result = page.post_comment(TEXT)

    assert result.state == COMMENT_CONFIRMED
    assert result.confirmed is True
    assert result.text == TEXT  # 확인된 문구를 그대로 들고 온다
    assert page._page.locator("#comments li").all_inner_texts() == [TEXT]


def test_새로고침_후에도_남아_있어야_확인된다(page: PlaywrightActionPage, dom_server: str):
    _open_clean(page, f"{dom_server}/reel.html")
    # 새로고침이 실제로 일어났는지 보기 위해 현재 document에만 표식을 남긴다.
    page._page.evaluate("window.__beforePost = true")

    result = page.post_comment(TEXT)

    assert result.confirmed is True
    assert page._page.evaluate("window.__beforePost === undefined") is True
    assert page._page.locator("#comments li").all_inner_texts() == [TEXT]


def test_댓글이_보이지_않으면_알_수_없는_상태로_남긴다(page: PlaywrightActionPage, dom_server: str):
    # 입력란만 비우고 댓글은 끝까지 보여주지 않는 fixture.
    _open_clean(page, f"{dom_server}/reel_comment_unverifiable.html")

    result = page.post_comment(TEXT)

    assert result.state == COMMENT_UNKNOWN_STATE
    assert result.detail == "not_in_comment_list"
    assert result.text == ""


def test_입력란이_없으면_아무것도_게시하지_않는다(page: PlaywrightActionPage, dom_server: str):
    _open_clean(page, f"{dom_server}/reel.html")
    page._page.evaluate("document.querySelector('#comment-form textarea').remove()")

    result = page.post_comment(TEXT)

    assert result.state == COMMENT_NOT_SENT
    assert result.detail == "input_not_found"
    assert _submit_clicks(page) == 0
    assert page._page.locator("#comments li").count() == 0


def test_게시_버튼이_없으면_아무것도_게시하지_않는다(page: PlaywrightActionPage, dom_server: str):
    _open_clean(page, f"{dom_server}/reel.html")
    page._page.evaluate("document.getElementById('submit').remove()")

    result = page.post_comment(TEXT)

    assert result.state == COMMENT_NOT_SENT
    assert result.detail == "submit_not_found"
    assert _submit_clicks(page) == 0


def test_확인에_실패해도_게시를_다시_시도하지_않는다(page: PlaywrightActionPage, dom_server: str):
    """중복 공개 댓글이 '알 수 없음'보다 나쁘므로 클릭은 정확히 1회여야 한다."""
    _open_clean(page, f"{dom_server}/reel_comment_unverifiable.html")

    result = page.post_comment(TEXT)

    assert result.state == COMMENT_UNKNOWN_STATE
    assert _submit_clicks(page) == 1


def test_확인에_성공해도_게시는_한_번만_누른다(page: PlaywrightActionPage, dom_server: str):
    _open_clean(page, f"{dom_server}/reel.html")

    assert page.comment(TEXT) is True  # 기존 Executor가 쓰는 래퍼도 그대로 동작한다

    assert _submit_clicks(page) == 1
    assert page._page.locator("#comments li").count() == 1  # 댓글도 1건뿐


def test_공백이_달라도_같은_댓글로_인정한다(page: PlaywrightActionPage, dom_server: str):
    """줄바꿈·연속 공백은 DOM 렌더링에서 접히므로 정규화 후 비교한다."""
    _open_clean(page, f"{dom_server}/reel.html")
    raw = "저도  퇴근하고\n카페 가고 싶네요"

    result = page.post_comment(raw)

    assert result.state == COMMENT_CONFIRMED
    rendered = page._page.locator("#comments li").all_inner_texts()
    assert rendered != [raw]  # DOM 문자열은 입력과 같지 않다
    assert result.text == normalize_comment_text(raw) == TEXT
