"""실제 Playwright + 실제 Chromium으로 selector를 검증한다 (Phase 18A.2).

Instagram에 접속하지 않는다. `tests/fixtures/instagram_dom/`의 로컬 HTML을
127.0.0.1 http 서버로 띄우고, 운영 코드가 쓰는 **실제 selector와 locator API**가
동작하는지 확인한다(가짜 locator가 아니라 진짜 selector 엔진으로).

Playwright 또는 Chromium이 없으면 건너뛴다(개발 환경 차이를 테스트 실패로 만들지 않는다).
"""
from __future__ import annotations

import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from targeting_agent.core.exceptions import SelectorMismatch
from targeting_agent.discovery.browser_diagnostics import collect_diagnostics
from targeting_agent.discovery.browser_models import SelectorStage, SessionState

pytest.importorskip("playwright.sync_api", reason="playwright 미설치")

from targeting_agent.discovery.browser_instagram import PlaywrightBrowser  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "instagram_dom"


def _executable_path() -> str:
    """고정 설치된 Chromium 경로(있으면 사용). 없으면 Playwright 기본값."""
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


@pytest.fixture(scope="module")
def browser(dom_server: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[PlaywrightBrowser]:
    page = PlaywrightBrowser(
        profile_dir=tmp_path_factory.mktemp("profile"),
        headless=True,
        timeout_ms=8000,
        selector_timeout_ms=1500,
        debug_dir=tmp_path_factory.mktemp("debug"),
        executable_path=_executable_path() or None,
        base_url=dom_server,
        scroll_rounds=1,
    )
    try:
        page.start()
    except Exception as exc:  # pragma: no cover - Chromium 미설치 환경
        pytest.skip(f"Chromium을 실행할 수 없습니다: {exc}")
    try:
        yield page
    finally:
        page.close()


def test_실제_chromium에서_공개_검색_주소로_결과_화면에_간다(browser: PlaywrightBrowser):
    """18A.6: 검색은 웹 UI가 스스로 이동하는 공개 결과 주소를 먼저 쓴다."""
    browser.search("직장인")

    assert "/explore/search/keyword/" in browser._page.url
    assert browser._page.locator('main a[href*="/p/"]').count() > 0


def test_실제_chromium에서_공개_주소가_막히면_ui로_되돌아간다(browser: PlaywrightBrowser, dom_server: str):
    """공개 주소에 결과 화면이 없으면 예전처럼 클릭·입력으로 간다."""
    original = browser.base_url
    # 홈과 태그 페이지는 있는데 공개 검색 주소만 없는 서버
    browser.base_url = f"{dom_server}/nosearchurl"
    try:
        browser.search("직장인")
    finally:
        browser.base_url = original

    # 해시태그 결과를 눌러 공개 태그 페이지에 도착했다.
    assert browser._page.url.endswith("tag.html")


def test_실제_chromium에서_reel_링크를_중복없이_수집한다(browser: PlaywrightBrowser, dom_server: str):
    browser.goto(f"{dom_server}/tag.html")

    links = browser.collect_post_links(10)

    # Reel 링크가 먼저 오고, 같은 그리드의 일반 게시물도 버리지 않는다(18A.6).
    assert links[:2] == [f"{dom_server}/reel/AAA111/", f"{dom_server}/reel/BBB222/"]
    assert links[2:] == [f"{dom_server}/p/POST01/"]


def test_실제_chromium에서_username과_caption을_읽는다(browser: PlaywrightBrowser, dom_server: str):
    post = browser.open_post(f"{dom_server}/reel.html")

    assert post is not None
    assert post.username == "office_daily_kim"
    assert "퇴근 후 카페" in post.caption
    assert post.hashtags == ["직장인", "퇴근후", "카페"]


def test_caption이_없는_화면은_빈_caption으로_읽는다(browser: PlaywrightBrowser, dom_server: str):
    post = browser.open_post(f"{dom_server}/reel_nocaption.html")

    assert post is not None
    assert post.username == "quiet_creator"
    assert post.caption == ""


def test_로그인_상태를_네비게이션_마커로_판정한다(browser: PlaywrightBrowser, dom_server: str):
    browser.goto(f"{dom_server}/")

    assert browser.session_state() is SessionState.LOGGED_IN


def test_요소가_없으면_stage와_진단과_스크린샷을_남긴다(browser: PlaywrightBrowser, dom_server: str):
    original = browser.base_url
    browser.base_url = f"{dom_server}/nosearch"  # 검색 진입 요소가 없는 홈 화면
    try:
        with pytest.raises(SelectorMismatch) as exc:
            browser.search("직장인")
    finally:
        browser.base_url = original

    assert exc.value.stage == SelectorStage.SEARCH_ENTRY.value
    assert "nav_search_aria_ko" in exc.value.tried
    shots = list((browser.debug_dir or Path(".")).glob("selector_search_entry_*.png"))
    assert shots, "실패 단계 스크린샷이 저장되어야 한다"


def test_진단은_구조만_수집하고_민감정보를_담지_않는다(browser: PlaywrightBrowser, dom_server: str):
    browser.goto(f"{dom_server}/")
    browser._page.locator("#search-entry").click()

    info = collect_diagnostics(browser._page, "SEARCH_INPUT")

    assert info["stage"] == "SEARCH_INPUT"
    assert info["title"] == "Instagram"
    assert {"placeholder": "검색", "aria_label": "검색 입력", "role": None, "type": None} in info[
        "textboxes"
    ]
    assert "/direct/inbox/" in info["nav_hrefs"]
    assert info["has_main"] is True
    # 수집 키는 구조 정보로 제한된다(html/cookie/token/localStorage 없음).
    assert set(info) == {
        "stage",
        "url",
        "title",
        "textboxes",
        "nav_hrefs",
        "reel_href_count",
        "has_main",
        "has_dialog",
    }


# ===========================================================================
# Phase 18A.5 — 실기에서 Found 10 → Collected 0 이던 원인 재현
# ===========================================================================
def test_실기_그리드_href는_username이_앞에_붙는다(browser: PlaywrightBrowser, dom_server: str):
    """태그/프로필 그리드의 Reel 링크는 /<username>/reel/<code>/ 형태다.

    이 형태를 Phase 12A 정규화가 거부해서 10건이 조용히 사라졌다(실기 증상).
    """
    from targeting_agent.discovery.url_input import normalize_instagram_url

    browser.goto(f"{dom_server}/tag_real.html")
    links = browser.collect_post_links(20)

    assert links, "그리드에서 링크를 찾아야 한다"
    # 실제 href 형태를 그대로 확인한다(절대 URL로 변환되어 있어야 한다).
    assert any("/office_daily_kim/reel/AAA111/" in link for link in links)
    # 그리고 그 형태가 canonical Reel URL로 정규화된다(Phase 18A.5 수정).
    # fixture는 127.0.0.1에서 제공되므로, 실기와 같은 조건으로 보려면 경로만 가져다 쓴다.
    canonical = set()
    for link in links:
        path = link.replace(dom_server, "")
        if "/reel/" not in path and "/reels/" not in path:
            continue
        canonical.add(normalize_instagram_url("https://www.instagram.com" + path).canonical_url)
    assert "https://www.instagram.com/reel/AAA111/" in canonical
    assert "https://www.instagram.com/reel/DDD444/" in canonical


# ===========================================================================
# Phase 18A.6 — 2026 화면에서 "Found 21 → Collected 0" 이던 원인 재현
# ===========================================================================
def test_2026_그리드는_p링크뿐이고_릴스_배지가_앞선다(browser: PlaywrightBrowser, dom_server: str):
    """검색 결과 그리드에는 /reel/ href가 하나도 없다(실기 21/21).

    /p/ 를 안 모으면 수집이 0건이 된다. 배지가 달린 타일을 앞에 둔다.
    """
    browser.goto(f"{dom_server}/explore/search/keyword/")

    links = browser.collect_post_links(10)

    assert links == [
        f"{dom_server}/p/AAA111/",
        f"{dom_server}/p/BBB222/",
        f"{dom_server}/p/PHOTO1/",
    ]


def test_2026_상세는_canonical과_메타태그로_읽는다(browser: PlaywrightBrowser, dom_server: str):
    """article/header/h1이 없는 화면에서도 작성자·본문·Reel 여부를 읽는다."""
    post = browser.open_post(f"{dom_server}/post_2026.html")

    assert post is not None
    assert post.canonical_url == "https://www.instagram.com/reel/AAA111/"
    assert post.media_type == "REEL"          # /p/ 로 열어도 canonical이 Reel이다
    assert post.username == "office_daily_kim"
    assert post.caption.startswith("퇴근 후 카페에서 마무리")
    assert post.hashtags == ["직장인", "퇴근후", "카페"]
    assert post.like_count == 9367 and post.comment_count == 62
    assert post.posted_at == "2026-08-23"  # Scorer의 activity 축이 '모름'을 벗어난다(18D.2)
    # 화면에 섞여 있는 댓글 텍스트를 caption으로 가져오지 않는다.
    assert "저도 그 카페" not in post.caption


def test_canonical이_일반_게시물이면_reel로_보지_않는다(browser: PlaywrightBrowser, dom_server: str):
    post = browser.open_post(f"{dom_server}/post_2026_photo.html")

    assert post is not None
    assert post.media_type == "POST"
    assert post.username == "photo_only_kim"


def test_상세_주소의_예약_경로를_작성자로_읽지_않는다(browser: PlaywrightBrowser, dom_server: str):
    """/reels/audio/... /explore/... 같은 링크는 프로필이 아니다."""
    post = browser.open_post(f"{dom_server}/post_2026.html")

    assert post is not None
    assert post.username not in {"reels", "explore"}
