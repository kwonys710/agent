"""Instagram Browser Discovery 테스트 (Phase 18A).

**실제 Instagram에 접속하지 않는다.** 모든 테스트는 FakeBrowser로만 동작하며,
Claude CLI / Windows Task Scheduler도 호출하지 않는다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from targeting_agent.core.config import Config
from targeting_agent.core.exceptions import BrowserSessionError
from targeting_agent.discovery.browser_instagram import InstagramBrowserDiscovery
from targeting_agent.discovery.browser_models import (
    SOURCE_BROWSER_SEARCH,
    PostDetail,
    SelectorStage,
    SessionState,
)
from targeting_agent.discovery.browser_runner import (
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_LOCKED,
    STATUS_PARTIAL,
    STATUS_SESSION_STOPPED,
    STATUS_SUCCESS,
    format_result,
    queries_from_config,
    run_browser_discovery,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


# --- Fake -----------------------------------------------------------------
class FakeBrowser:
    """읽기 전용 브라우저 대역. 쓰기 동작(like/comment/follow)은 존재하지 않는다."""

    def __init__(
        self,
        links: Optional[dict[str, list[str]]] = None,
        details: Optional[dict[str, PostDetail]] = None,
        state: SessionState = SessionState.LOGGED_IN,
        fail_queries: tuple[str, ...] = (),
    ) -> None:
        self.links = links or {}
        self.details = details or {}
        self.state = state
        self.fail_queries = fail_queries
        self.calls: list[tuple] = []
        self.debug: list[str] = []
        self.closed = False

    def session_state(self) -> SessionState:
        self.calls.append(("session_state",))
        return self.state

    def search(self, query: str) -> None:
        self.calls.append(("search", query))
        if query in self.fail_queries:
            raise RuntimeError("locator not found")

    def collect_post_links(self, limit: int) -> list[str]:
        query = next(q for kind, q in reversed(self.calls) if kind == "search")
        self.calls.append(("collect", query, limit))
        return list(self.links.get(query, []))[:limit]

    def open_post(self, url: str) -> Optional[PostDetail]:
        self.calls.append(("open_post", url))
        return self.details.get(url)

    def capture_debug(self, name: str) -> Optional[str]:
        self.debug.append(name)
        return None

    def close(self) -> None:
        self.closed = True


def detail(code: str, caption: str = "퇴근 후 카페 #직장인 #일상") -> PostDetail:
    url = f"https://www.instagram.com/reel/{code}/"
    return PostDetail(
        permalink=url,
        username=f"user_{code.lower()}",
        caption=caption,
        hashtags=["직장인", "일상"],
        media_type="REEL",
        like_count=120,
        comment_count=7,
    )


def browser_with(codes: list[str], query: str = "직장인") -> FakeBrowser:
    urls = [f"https://www.instagram.com/reel/{c}/" for c in codes]
    return FakeBrowser(
        links={query: urls}, details={u: detail(c) for u, c in zip(urls, codes)}
    )


@pytest.fixture()
def discovery_config(config: Config, tmp_path: Path) -> Config:
    """브라우저 Discovery를 켠 테스트 설정(경로는 모두 tmp_path 아래)."""
    raw = {
        **config.raw,
        "browser_discovery": {
            "enabled": True,
            "headless": True,
            "profile_dir": "browser_profile",
            "queries": ["직장인"],
            "media_types": ["REEL"],
            "limits": {
                "max_queries_per_run": 5,
                "max_candidates_per_query": 10,
                "max_candidates_per_run": 40,
                "max_analyze_per_run": 20,
            },
            "stop_on": {"login_required": True, "challenge": True, "platform_warning": True},
            "lock": {"enabled": True, "path": "data/.browser_discovery.lock", "stale_minutes": 30},
            "debug": {"enabled": False, "dir": "browser_debug", "max_screenshots": 10},
        },
    }
    return Config(raw=raw, path=config.path, base_dir=tmp_path)


class FakeUsage:
    claude_calls = 1
    cache_hits = 2


class FakeIntelligence:
    usage = FakeUsage()


class FakeResult:
    def __init__(self, status: str = "ANALYZED", detail: str = "") -> None:
        self.status = status
        self.detail = detail


class FakeProcessor:
    """CandidateProcessor 대역. Claude를 호출하지 않는다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.processed: list[int] = []
        self.intelligence = FakeIntelligence()

    def process(self, media_pk: int) -> FakeResult:
        self.processed.append(media_pk)
        return FakeResult()


# --- Discovery 단위 --------------------------------------------------------
def test_logged_in_세션이면_수집한다():
    browser = browser_with(["AAA111", "BBB222"])
    discovery = InstagramBrowserDiscovery(browser, ["직장인"])

    candidates = discovery.discover()

    assert len(candidates) == 2
    assert all(c.source == SOURCE_BROWSER_SEARCH for c in candidates)
    assert candidates[0].permalink == "https://www.instagram.com/reel/AAA111/"
    assert candidates[0].username == "user_aaa111"


@pytest.mark.parametrize(
    "state",
    [SessionState.LOGIN_REQUIRED, SessionState.CHALLENGE, SessionState.PLATFORM_WARNING],
)
def test_중단_상태면_즉시_예외로_멈춘다(state: SessionState):
    browser = FakeBrowser(state=state)
    discovery = InstagramBrowserDiscovery(browser, ["직장인"])

    with pytest.raises(BrowserSessionError) as exc:
        discovery.discover()

    assert exc.value.state == state.value
    # 검색조차 시작하지 않는다 — 세션 확인에서 끝난다.
    assert not any(call[0] == "search" for call in browser.calls)


def test_unknown_상태도_기본적으로_중단한다():
    browser = FakeBrowser(state=SessionState.UNKNOWN)
    discovery = InstagramBrowserDiscovery(browser, ["직장인"])

    with pytest.raises(BrowserSessionError):
        discovery.discover()


def test_중복_url은_한_번만_수집한다():
    url = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={"직장인": [url, url + "?utm_source=ig_web", url]},
        details={url: detail("AAA111")},
    )
    discovery = InstagramBrowserDiscovery(browser, ["직장인"])

    candidates = discovery.discover()

    assert len(candidates) == 1


def test_reel이_아니면_제외한다():
    post = "https://www.instagram.com/p/POST01/"
    reel = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={"직장인": [post, reel]},
        details={post: detail("POST01"), reel: detail("AAA111")},
    )
    discovery = InstagramBrowserDiscovery(browser, ["직장인"], media_types=("REEL",))

    candidates = discovery.discover()

    assert [c.media_id for c in candidates] == ["AAA111"]


def test_지원하지_않는_url은_조용히_건너뛴다():
    browser = FakeBrowser(
        links={"직장인": ["https://example.com/reel/X/", "https://www.instagram.com/reel/AAA111/"]},
        details={"https://www.instagram.com/reel/AAA111/": detail("AAA111")},
    )
    discovery = InstagramBrowserDiscovery(browser, ["직장인"])

    assert len(discovery.discover()) == 1


def test_실행당_검색어_수를_제한한다():
    browser = FakeBrowser(links={q: [] for q in ["a", "b", "c", "d"]})
    discovery = InstagramBrowserDiscovery(browser, ["a", "b", "c", "d"], max_queries_per_run=2)

    discovery.discover()

    assert [c[1] for c in browser.calls if c[0] == "search"] == ["a", "b"]


def test_검색어당_후보_수를_제한한다():
    browser = browser_with([f"CODE{i:03d}" for i in range(10)])
    discovery = InstagramBrowserDiscovery(browser, ["직장인"], max_candidates_per_query=3)

    assert len(discovery.discover()) == 3


def test_실행당_후보_총량을_제한한다():
    codes_a = [f"A{i:03d}" for i in range(5)]
    codes_b = [f"B{i:03d}" for i in range(5)]
    urls = {
        "a": [f"https://www.instagram.com/reel/{c}/" for c in codes_a],
        "b": [f"https://www.instagram.com/reel/{c}/" for c in codes_b],
    }
    details = {
        f"https://www.instagram.com/reel/{c}/": detail(c) for c in codes_a + codes_b
    }
    browser = FakeBrowser(links=urls, details=details)
    discovery = InstagramBrowserDiscovery(browser, ["a", "b"], max_candidates_per_run=6)

    assert len(discovery.discover()) == 6


def test_selector_실패는_해당_검색어만_건너뛴다():
    good = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={"good": [good]}, details={good: detail("AAA111")}, fail_queries=("bad",)
    )
    discovery = InstagramBrowserDiscovery(browser, ["bad", "good"])

    candidates = discovery.discover()

    assert len(candidates) == 1
    assert discovery.stats.selector_errors == 1
    # 실패 기록에 stage / selector key / 예외 타입이 모두 보인다.
    message = discovery.stats.queries[0].errors[0]
    assert "stage=" in message and "type=RuntimeError" in message
    assert discovery.stats.queries[0].failed_stage is not None


def test_상세_화면을_읽지_못하면_후보로_만들지_않는다():
    url = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(links={"직장인": [url]}, details={})
    discovery = InstagramBrowserDiscovery(browser, ["직장인"])

    assert discovery.discover() == []
    assert discovery.stats.selector_errors == 1
    assert browser.debug  # 디버그 스크린샷 시도(실패 원인 추적용)


def test_캡션이_없으면_해시태그도_비어_있다():
    url = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={"직장인": [url]},
        details={url: PostDetail(permalink=url, username="u", caption="")},
    )
    discovery = InstagramBrowserDiscovery(browser, ["직장인"])

    candidate = discovery.discover()[0]
    assert candidate.caption == ""
    assert candidate.hashtags == []


def test_브라우저는_읽기_동작만_호출한다():
    browser = browser_with(["AAA111"])
    InstagramBrowserDiscovery(browser, ["직장인"]).discover()

    used = {call[0] for call in browser.calls}
    assert used <= {"session_state", "search", "collect", "open_post"}
    for forbidden in ("like", "comment", "follow", "send_dm", "save", "share"):
        assert not hasattr(browser, forbidden)
        assert not hasattr(InstagramBrowserDiscovery, forbidden)


# --- Runner ---------------------------------------------------------------
def test_비활성화면_아무것도_하지_않는다(config: Config, tmp_path: Path):
    disabled = Config(
        raw={**config.raw, "browser_discovery": {"enabled": False}},
        path=config.path,
        base_dir=tmp_path,
    )
    result = run_browser_discovery(disabled, browser=FakeBrowser())

    assert result.status == STATUS_DISABLED
    assert result.exit_code == 0


def test_lock이_잡혀_있으면_종료한다(discovery_config: Config, conn: sqlite3.Connection, tmp_path: Path):
    lock_path = tmp_path / "data" / ".browser_discovery.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    lock_path.write_text(
        '{"pid": 1, "created_at": "%s"}' % datetime.now(timezone.utc).isoformat(timespec="seconds"),
        encoding="utf-8",
    )

    result = run_browser_discovery(discovery_config, conn, browser=browser_with(["AAA111"]))

    assert result.status == STATUS_LOCKED
    assert result.added == 0


def test_수집한_후보를_기존_ingest로_저장한다(discovery_config: Config, conn: sqlite3.Connection):
    result = run_browser_discovery(
        discovery_config,
        conn,
        browser=browser_with(["AAA111", "BBB222"]),
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert result.status == STATUS_SUCCESS
    assert result.added == 2
    rows = conn.execute(
        "SELECT source, canonical_url FROM candidate_media ORDER BY media_pk"
    ).fetchall()
    assert [r["source"] for r in rows] == [SOURCE_BROWSER_SEARCH] * 2


def test_이미_있는_후보는_중복으로_처리하고_다시_분석하지_않는다(
    discovery_config: Config, conn: sqlite3.Connection
):
    processors: list[FakeProcessor] = []

    def factory(cfg, c):
        processor = FakeProcessor(c)
        processors.append(processor)
        return processor

    run_browser_discovery(
        discovery_config, conn, browser=browser_with(["AAA111"]), processor_factory=factory
    )
    second = run_browser_discovery(
        discovery_config, conn, browser=browser_with(["AAA111"]), processor_factory=factory
    )

    assert second.added == 0
    assert second.duplicate == 1
    assert len(processors) == 1  # 두 번째 실행은 분석기를 만들지도 않는다


def test_discover_only는_분석하지_않는다(discovery_config: Config, conn: sqlite3.Connection):
    processors: list[FakeProcessor] = []

    def factory(cfg, c):
        processors.append(FakeProcessor(c))
        return processors[-1]

    result = run_browser_discovery(
        discovery_config,
        conn,
        browser=browser_with(["AAA111"]),
        processor_factory=factory,
        process=False,
    )

    assert result.added == 1
    assert result.analyzed == 0
    assert processors == []


def test_세션_중단이면_후보를_저장하지_않는다(discovery_config: Config, conn: sqlite3.Connection):
    result = run_browser_discovery(
        discovery_config, conn, browser=FakeBrowser(state=SessionState.CHALLENGE)
    )

    assert result.status == STATUS_SESSION_STOPPED
    assert result.stop_reason == SessionState.CHALLENGE.value
    assert result.exit_code == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM candidate_media").fetchone()["n"] == 0


def test_실행_이력을_남긴다(discovery_config: Config, conn: sqlite3.Connection):
    run_browser_discovery(
        discovery_config,
        conn,
        browser=browser_with(["AAA111"]),
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    row = conn.execute(
        "SELECT status, session_state, found, collected, added, claude_calls "
        "FROM browser_discovery_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == STATUS_SUCCESS
    assert row["session_state"] == SessionState.LOGGED_IN.value
    assert row["added"] == 1
    assert row["claude_calls"] == 1


def test_중단된_실행도_이력에_남는다(discovery_config: Config, conn: sqlite3.Connection):
    run_browser_discovery(discovery_config, conn, browser=FakeBrowser(state=SessionState.LOGIN_REQUIRED))

    row = conn.execute(
        "SELECT status, stop_reason FROM browser_discovery_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == STATUS_SESSION_STOPPED
    assert row["stop_reason"] == SessionState.LOGIN_REQUIRED.value


def test_분석_대상은_신규_후보로만_제한된다(discovery_config: Config, conn: sqlite3.Connection):
    raw = {**discovery_config.raw}
    raw["browser_discovery"] = {
        **raw["browser_discovery"],
        "limits": {**raw["browser_discovery"]["limits"], "max_analyze_per_run": 2},
    }
    limited = Config(raw=raw, path=discovery_config.path, base_dir=discovery_config.base_dir)
    holder: list[FakeProcessor] = []

    def factory(cfg, c):
        holder.append(FakeProcessor(c))
        return holder[-1]

    result = run_browser_discovery(
        limited,
        conn,
        browser=browser_with(["AAA111", "BBB222", "CCC333"]),
        processor_factory=factory,
    )

    assert result.added == 3
    assert len(holder[0].processed) == 2  # 한도 초과분은 다음 실행(또는 Dashboard)에서 처리


def test_검색어가_없으면_브라우저를_열지_않는다(discovery_config: Config, conn: sqlite3.Connection):
    raw = {**discovery_config.raw}
    raw["browser_discovery"] = {**raw["browser_discovery"], "queries": []}
    raw["discovery"] = {**raw.get("discovery", {}), "hashtags": []}
    empty = Config(raw=raw, path=discovery_config.path, base_dir=discovery_config.base_dir)
    opened: list[str] = []

    result = run_browser_discovery(
        empty, conn, browser_factory=lambda cfg: opened.append("x") or FakeBrowser()
    )

    assert opened == []
    assert result.added == 0


def test_검색어는_설정에서_읽고_없으면_해시태그를_쓴다(config: Config, tmp_path: Path):
    explicit = Config(
        raw={**config.raw, "browser_discovery": {"queries": ["a", " b "]}},
        path=config.path,
        base_dir=tmp_path,
    )
    assert queries_from_config(explicit) == ["a", "b"]

    fallback = Config(
        raw={**config.raw, "browser_discovery": {"queries": []}},
        path=config.path,
        base_dir=tmp_path,
    )
    assert queries_from_config(fallback) == [
        str(h) for h in config.get("discovery.hashtags", [])
    ]


def test_실행_후_브라우저를_닫는다(discovery_config: Config, conn: sqlite3.Connection):
    browser = browser_with(["AAA111"])
    run_browser_discovery(
        discovery_config,
        conn,
        browser_factory=lambda cfg: browser,
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert browser.closed is True


def test_요약_출력에_중단_사유가_보인다(discovery_config: Config, conn: sqlite3.Connection):
    result = run_browser_discovery(
        discovery_config, conn, browser=FakeBrowser(state=SessionState.PLATFORM_WARNING)
    )

    text = format_result(result)
    assert "PLATFORM_WARNING" in text
    assert "Browser Discovery" in text


# --- 보안 ------------------------------------------------------------------
def code_only(path: Path) -> str:
    """주석과 docstring을 뺀 실제 코드만 돌려준다(설명문이 검사에 걸리지 않도록)."""
    import io
    import tokenize

    pieces: list[str] = []
    previous = tokenize.NEWLINE
    for token in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENCODING,
        ):
            continue  # docstring
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            pieces.append(token.string)
        previous = token.type
    return " ".join(pieces)


BROWSER_FILES = [
    PACKAGE_ROOT / "discovery" / "browser_instagram.py",
    PACKAGE_ROOT / "discovery" / "browser_runner.py",
    PACKAGE_ROOT / "discovery" / "browser_selectors.py",
    PACKAGE_ROOT / "scripts" / "init_instagram_session.py",
]


def test_자격증명을_다루는_코드가_없다():
    banned = ("IG_PASSWORD", "INSTAGRAM_PASSWORD", "password", "getpass")
    for path in BROWSER_FILES:
        text = code_only(path)
        for token in banned:
            assert token not in text, f"{path.name}에 자격증명 처리 흔적: {token}"


def test_쿠키_세션_추출_코드가_없다():
    banned = ("storage_state", "cookies", "add_cookies", "localStorage")
    for path in BROWSER_FILES:
        text = code_only(path)
        for token in banned:
            assert token not in text, f"{path.name}에 세션 추출 흔적: {token}"


def test_우회_기법_코드가_없다():
    banned = ("stealth", "webdriver", "user_agent", "proxy", "graphql", "set_extra_http_headers")
    for path in BROWSER_FILES:
        text = code_only(path)
        for token in banned:
            assert token not in text, f"{path.name}에 우회 흔적: {token}"


def test_쓰기_동작_코드가_없다():
    banned = ("likes", "unfollow", "send_dm", "like_post", "post_comment")
    for path in BROWSER_FILES:
        text = code_only(path)
        for token in banned:
            assert token not in text, f"{path.name}에 쓰기 동작 흔적: {token}"


def test_config에_자격증명_항목이_없다():
    text = (PACKAGE_ROOT / "config.yaml").read_text(encoding="utf-8")
    block = text.split("browser_discovery:", 1)[1].split("\nscheduler:", 1)[0]
    for token in ("password", "pw:", "username:", "token"):
        assert token not in block.lower()


def test_scheduler와_자동_연결되지_않는다():
    text = (PACKAGE_ROOT / "scheduler" / "runner.py").read_text(encoding="utf-8")
    assert "browser_runner" not in text
    assert "browser_discovery" not in text


# ===========================================================================
# Phase 18A.1 — DOM / Selector 호환성
# ===========================================================================
from targeting_agent.core.exceptions import SelectorMismatch  # noqa: E402
from targeting_agent.discovery import browser_selectors as SEL  # noqa: E402
from targeting_agent.discovery.browser_instagram import PlaywrightBrowser  # noqa: E402


class FakeTimeout(Exception):
    """Playwright TimeoutError 대역."""


class FakeLocator:
    """selector 하나에 매칭된 가짜 노드 묶음."""

    def __init__(self, page: "FakePage", selector: str, nodes: list[dict]) -> None:
        self.page = page
        self.selector = selector
        self.nodes = nodes

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return len(self.nodes)

    def wait_for(self, state: str = "visible", timeout: Optional[int] = None) -> None:
        if not self.nodes:
            raise FakeTimeout(f"Timeout {timeout}ms exceeded: {self.selector}")

    def is_visible(self, timeout: Optional[int] = None) -> bool:
        return bool(self.nodes)

    def click(self, timeout: Optional[int] = None) -> None:
        self.wait_for(timeout=timeout)
        self.page.clicked.append(self.selector)

    def fill(self, value: str, timeout: Optional[int] = None) -> None:
        self.wait_for(timeout=timeout)
        self.page.filled.append((self.selector, value))

    def inner_text(self, timeout: Optional[int] = None) -> str:
        self.wait_for(timeout=timeout)
        return str(self.nodes[0].get("text", ""))

    def get_attribute(self, name: str, timeout: Optional[int] = None) -> Optional[str]:
        self.wait_for(timeout=timeout)
        return self.nodes[0].get(name)

    def evaluate_all(self, _expression: str) -> list[Optional[str]]:
        return [node.get("href") for node in self.nodes]


class FakeMouse:
    def __init__(self) -> None:
        self.scrolls = 0

    def wheel(self, _x: int, _y: int) -> None:
        self.scrolls += 1


class FakePage:
    """selector → 노드 목록으로 이뤄진 최소 DOM. 실제 브라우저를 쓰지 않는다."""

    def __init__(self, dom: dict[str, list[dict]], gated: tuple[str, ...] = ()) -> None:
        self.dom = dom
        self.gated = set(gated)  # 검색어를 입력해야 나타나는 selector
        self.url = SEL.BASE_URL + "/"
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.visited: list[str] = []
        self.mouse = FakeMouse()
        self.screenshots: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        if selector in self.gated and not self.filled:
            return FakeLocator(self, selector, [])
        return FakeLocator(self, selector, list(self.dom.get(selector, [])))

    def goto(self, url: str, **_kwargs) -> None:
        self.visited.append(url)
        self.url = url

    def wait_for_load_state(self, _state: str = "load") -> None:
        pass

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def inner_text(self, _selector: str) -> str:
        return ""

    def set_default_timeout(self, _ms: int) -> None:
        pass

    def screenshot(self, path: str) -> None:
        self.screenshots.append(path)


def make_browser(page: FakePage, tmp_path: Path, **kwargs) -> PlaywrightBrowser:
    browser = PlaywrightBrowser(
        profile_dir=tmp_path / "profile",
        headless=True,
        selector_timeout_ms=10,
        debug_dir=tmp_path / "debug",
        **kwargs,
    )
    browser._page = page
    return browser


def search_dom(entry: str, input_selector: str) -> dict[str, list[dict]]:
    """검색 진입 → 입력 → 해시태그 결과 → 결과 페이지까지의 최소 DOM."""
    return {
        entry: [{"text": "검색"}],
        input_selector: [{"text": ""}],
        SEL.SEARCH_RESULT_HASHTAG[0][1]: [{"href": "/explore/tags/직장인/"}],
        SEL.RESULT_PAGE_MARKERS[0][1]: [{"href": "/reel/AAA111/"}],
    }


def test_한국어_ui_검색_라벨로_진입한다(tmp_path: Path):
    entry = dict(SEL.SEARCH_ENTRY)["nav_search_aria_ko"]
    box = dict(SEL.SEARCH_INPUT)["input_placeholder_ko"]
    page = FakePage(search_dom(entry, box), gated=(SEL.SEARCH_RESULT_HASHTAG[0][1],))

    make_browser(page, tmp_path).search("직장인")

    assert page.clicked[0] == entry
    assert page.filled == [(box, "직장인")]


def test_영어_ui_검색_라벨로도_진입한다(tmp_path: Path):
    entry = dict(SEL.SEARCH_ENTRY)["nav_search_aria_en"]
    box = dict(SEL.SEARCH_INPUT)["input_placeholder_en"]
    page = FakePage(search_dom(entry, box), gated=(SEL.SEARCH_RESULT_HASHTAG[0][1],))

    make_browser(page, tmp_path).search("직장인")

    assert page.clicked[0] == entry


def test_검색_입력_selector는_대체안으로_넘어간다(tmp_path: Path):
    entry = dict(SEL.SEARCH_ENTRY)["nav_search_svg_ko"]
    box = dict(SEL.SEARCH_INPUT)["input_role_searchbox"]  # placeholder 계열이 없는 경우
    page = FakePage(search_dom(entry, box), gated=(SEL.SEARCH_RESULT_HASHTAG[0][1],))

    make_browser(page, tmp_path).search("직장인")

    assert page.filled == [(box, "직장인")]


def test_해시태그_결과가_없으면_계정_결과로_넘어간다(tmp_path: Path):
    entry = dict(SEL.SEARCH_ENTRY)["nav_search_aria_ko"]
    box = dict(SEL.SEARCH_INPUT)["input_placeholder_ko"]
    account = dict(SEL.SEARCH_RESULT_ACCOUNT)["result_account_link"]
    dom = {
        entry: [{"text": "검색"}],
        box: [{"text": ""}],
        account: [{"href": "/office_daily_kim/"}],
        SEL.RESULT_PAGE_MARKERS[0][1]: [{"href": "/reel/AAA111/"}],
    }
    page = FakePage(dom, gated=(account,))

    make_browser(page, tmp_path).search("직장인")

    assert account in page.clicked


def test_검색_입력을_못_찾으면_stage와_selector키를_알려준다(tmp_path: Path):
    entry = dict(SEL.SEARCH_ENTRY)["nav_search_aria_ko"]
    page = FakePage({entry: [{"text": "검색"}]})  # 입력 필드 없음
    browser = make_browser(page, tmp_path)

    with pytest.raises(SelectorMismatch) as exc:
        browser.search("직장인")

    assert exc.value.stage == SelectorStage.SEARCH_INPUT.value
    assert "input_placeholder_ko" in exc.value.tried
    assert page.screenshots and "search_input" in page.screenshots[0]


def test_검색_진입_실패도_stage로_구분된다(tmp_path: Path):
    browser = make_browser(FakePage({}), tmp_path)

    with pytest.raises(SelectorMismatch) as exc:
        browser.search("직장인")

    assert exc.value.stage == SelectorStage.SEARCH_ENTRY.value


def test_reel_href만_수집하고_중복을_제거한다(tmp_path: Path):
    selector = dict(SEL.POST_LINK_SELECTORS)["reel_href"]
    page = FakePage(
        {
            selector: [
                {"href": "/reel/AAA111/"},
                {"href": "/reel/AAA111/"},  # 같은 링크가 두 번 보이는 경우
                {"href": "https://www.instagram.com/reel/BBB222/"},
            ]
        }
    )

    links = make_browser(page, tmp_path).collect_post_links(10)

    assert links == [
        "https://www.instagram.com/reel/AAA111/",
        "https://www.instagram.com/reel/BBB222/",
    ]


def test_reel_링크가_하나도_없으면_stage를_알려준다(tmp_path: Path):
    browser = make_browser(FakePage({}), tmp_path, scroll_rounds=1)

    with pytest.raises(SelectorMismatch) as exc:
        browser.collect_post_links(10)

    assert exc.value.stage == SelectorStage.REEL_LINK.value


def test_상세에서_username과_caption을_읽는다(tmp_path: Path):
    username_sel = dict(SEL.USERNAME_SELECTORS)["username_header_link"]
    caption_sel = dict(SEL.CAPTION_SELECTORS)["caption_h1"]
    page = FakePage(
        {
            username_sel: [{"href": "/office_daily_kim/"}],
            caption_sel: [{"text": "퇴근 후 카페 #직장인 #일상"}],
        }
    )

    post = make_browser(page, tmp_path).open_post("https://www.instagram.com/reel/AAA111/")

    assert post is not None
    assert post.username == "office_daily_kim"
    assert post.caption == "퇴근 후 카페 #직장인 #일상"
    assert post.hashtags == ["직장인", "일상"]


def test_caption을_못_찾으면_빈_값으로_두고_추측하지_않는다(tmp_path: Path):
    username_sel = dict(SEL.USERNAME_SELECTORS)["username_header_link"]
    page = FakePage({username_sel: [{"href": "/office_daily_kim/"}]})

    post = make_browser(page, tmp_path).open_post("https://www.instagram.com/reel/AAA111/")

    assert post is not None
    assert post.caption == ""
    assert post.hashtags == []


def test_상세를_전혀_못_읽으면_none과_스크린샷(tmp_path: Path):
    page = FakePage({})
    browser = make_browser(page, tmp_path)

    assert browser.open_post("https://www.instagram.com/reel/AAA111/") is None
    assert page.screenshots and "post_detail" in page.screenshots[0]


def test_caption이_없으면_needs_enrichment로_저장된다(
    discovery_config: Config, conn: sqlite3.Connection
):
    url = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={"직장인": [url]},
        details={url: PostDetail(permalink=url, username="office_daily_kim", caption="")},
    )

    result = run_browser_discovery(
        discovery_config, conn, browser=browser, processor_factory=lambda cfg, c: FakeProcessor(c)
    )

    assert result.added == 1
    status = conn.execute("SELECT status FROM candidate_media").fetchone()["status"]
    assert status == "NEEDS_ENRICHMENT"


# --- Run 상태 --------------------------------------------------------------
def test_모든_검색어_성공이면_success(discovery_config: Config, conn: sqlite3.Connection):
    result = run_browser_discovery(
        discovery_config,
        conn,
        browser=browser_with(["AAA111"]),
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert result.status == STATUS_SUCCESS
    assert (result.ok_queries, result.failed_queries) == (1, 0)


def test_일부_검색어만_실패하면_partial(discovery_config: Config, conn: sqlite3.Connection):
    good = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={"good": [good]}, details={good: detail("AAA111")}, fail_queries=("bad",)
    )

    result = run_browser_discovery(
        discovery_config,
        conn,
        browser=browser,
        queries=["bad", "good"],
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert result.status == STATUS_PARTIAL
    assert (result.ok_queries, result.failed_queries) == (1, 1)
    assert result.exit_code == 0


def test_모든_검색어가_실패하면_failed(discovery_config: Config, conn: sqlite3.Connection):
    browser = FakeBrowser(fail_queries=("a", "b"))

    result = run_browser_discovery(discovery_config, conn, browser=browser, queries=["a", "b"])

    assert result.status == STATUS_FAILED
    assert result.failed_queries == 2
    assert result.exit_code == 1
    row = conn.execute(
        "SELECT status FROM browser_discovery_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == STATUS_FAILED


def test_오류_합계가_세부_건수와_일치한다(discovery_config: Config, conn: sqlite3.Connection):
    browser = FakeBrowser(fail_queries=("a", "b"))

    result = run_browser_discovery(discovery_config, conn, browser=browser, queries=["a", "b"])
    text = format_result(result)

    assert result.selector_errors == 2
    assert result.other_errors == 0
    assert result.errors == result.selector_errors + result.other_errors
    assert " 오류        : 2" in text
    assert "- Selector : 2" in text


def test_검색어를_하나만_주면_하나만_실행한다(discovery_config: Config, conn: sqlite3.Connection):
    from targeting_agent.main import build_parser

    args = build_parser().parse_args(["--discover-only", "--query", "직장인"])
    assert args.query == ["직장인"]

    # config에는 기본 검색어가 5개 들어 있는 상태를 만든다.
    raw = {**discovery_config.raw}
    raw["browser_discovery"] = {
        **raw["browser_discovery"],
        "queries": ["직장인", "직장인브이로그", "회사원", "출근", "퇴근"],
    }
    many = Config(raw=raw, path=discovery_config.path, base_dir=discovery_config.base_dir)
    assert len(queries_from_config(many)) == 5

    browser = browser_with(["AAA111"])
    result = run_browser_discovery(
        many, conn, browser=browser, queries=args.query, process=False
    )
    assert result.queries == 1
    assert [c[1] for c in browser.calls if c[0] == "search"] == ["직장인"]
    assert result.claude_calls == 0


def test_discover_only는_instagram_동작이_0이다(discovery_config: Config, conn: sqlite3.Connection):
    browser = browser_with(["AAA111"])

    run_browser_discovery(discovery_config, conn, browser=browser, process=False)

    used = {call[0] for call in browser.calls}
    assert used <= {"session_state", "search", "collect", "open_post"}
    assert conn.execute("SELECT COUNT(*) AS n FROM action_queue").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM interactions").fetchone()["n"] == 0
