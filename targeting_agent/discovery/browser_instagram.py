"""Instagram Browser Discovery (Phase 18A / 18A.1).

운영자가 **직접 로그인한** 브라우저 프로필을 재사용해 Instagram 웹 화면을 열고,
검색 결과에서 Reel 후보를 찾아 공개 DOM 정보만 읽는다.

검색은 공개 Web UI navigation 순서를 그대로 따른다(18A.1).

    홈 → 검색 진입 → 검색 입력 → 결과 목록 → 공개 결과 페이지(해시태그/프로필)
    → /reel/ 링크 수집 → 게시물 열기 → username/caption 읽기

각 단계는 `SelectorStage`로 구분되고, 실패하면 "어느 단계에서 어떤 selector 키가
안 맞았는지"를 그대로 남긴다. 전체 HTML dump는 하지 않는다(스크린샷 1장만).

하지 않는 것(구현 자체를 두지 않는다):
- 좋아요 / 댓글 / 팔로우 / DM / 저장 / 공유 등 **모든 쓰기 동작**
- CAPTCHA·Challenge·경고·Rate Limit 우회, stealth/fingerprint/proxy/계정 rotation
- 비공식 API·GraphQL endpoint 호출, network 응답 가로채기
- ID/PW 자동 입력, 쿠키·세션 토큰 추출/저장
- 사람처럼 보이기 위한 랜덤 지연(로딩 대기만 사용)

로그인 필요/Challenge/경고가 감지되면 **즉시 중단**한다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from ..core.exceptions import BrowserSessionError, DiscoveryError, SelectorMismatch
from ..core.logger import get_logger
from ..core.models import RawCandidate
from .base import DiscoverySource, extract_hashtags
from .browser_models import (
    SOURCE_BROWSER_SEARCH,
    DiscoveryStats,
    PostDetail,
    QueryResult,
    SelectorStage,
    SessionState,
)
from .browser_selectors import (
    BASE_URL,
    CAPTION_SELECTORS,
    CHALLENGE_TEXTS,
    HOME_URL,
    LOGGED_IN_MARKERS,
    LOGIN_REQUIRED_MARKERS,
    LOGIN_TEXTS,
    POST_LINK_SELECTORS,
    RESULT_PAGE_MARKERS,
    SEARCH_ENTRY,
    SEARCH_INPUT,
    SEARCH_RESULT_ACCOUNT,
    SEARCH_RESULT_HASHTAG,
    USERNAME_SELECTORS,
    WARNING_TEXTS,
    keys_of,
)
from .url_input import candidate_from_url, normalize_instagram_url

logger = get_logger("discovery.browser")


class BrowserPage(Protocol):
    """Discovery가 사용하는 브라우저 동작(읽기 전용).

    테스트는 이 Protocol을 구현한 Fake로 대체한다 — 실제 Instagram에 접속하지 않는다.
    """

    def session_state(self) -> SessionState: ...

    def search(self, query: str) -> None: ...

    def collect_post_links(self, limit: int) -> list[str]: ...

    def open_post(self, url: str) -> Optional[PostDetail]: ...

    def capture_debug(self, name: str) -> Optional[str]: ...

    def close(self) -> None: ...


# --- Playwright 구현 -------------------------------------------------------
@dataclass
class PlaywrightBrowser:
    """운영자가 로그인한 persistent profile을 그대로 쓰는 Playwright 브라우저.

    playwright는 지연 import한다(미설치 환경에서도 나머지 기능이 동작해야 한다).

    대기 시간은 두 가지로 나눈다(18A.1).
    - `timeout_ms`        : 페이지 이동/로딩 대기 — 넉넉해야 한다
    - `selector_timeout_ms`: selector 하나를 확인하는 시간 — 짧아야 한다
      (안 맞는 selector 하나에 20초씩 쓰면 검색어 5개에 100초가 사라진다)
    """

    profile_dir: Path
    headless: bool = False
    timeout_ms: int = 20000
    selector_timeout_ms: int = 4000
    debug_dir: Optional[Path] = None
    max_screenshots: int = 10
    scroll_rounds: int = 3
    _playwright: Any = None
    _context: Any = None
    _page: Any = None
    _screenshots: int = 0

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 설치 여부에 따른 분기
            raise DiscoveryError(
                "playwright가 설치되어 있지 않습니다. "
                "pip install playwright && python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        # persistent context = 운영자가 직접 로그인한 세션을 그대로 재사용한다.
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    # --- 공통 도우미 -----------------------------------------------------
    def goto(self, url: str) -> None:
        page = self._require_page()
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

    def _locate(self, stage: SelectorStage, entries: Sequence[tuple[str, str]]) -> tuple[str, Any]:
        """단계 하나에 **대기 예산을 한 번만** 쓴다.

        selector마다 timeout을 걸면 후보 7개 × 4초 = 28초가 되어 오히려 느려진다.
        그래서 후보 전체를 즉시 확인(count/is_visible)하고, 없으면 잠깐 기다렸다가
        다시 확인하는 방식으로 `selector_timeout_ms` 안에서 끝낸다.
        """
        page = self._require_page()
        deadline = time.monotonic() + max(self.selector_timeout_ms, 200) / 1000
        last_error = ""
        while True:
            for key, selector in entries:
                locator = page.locator(selector).first
                try:
                    if locator.count() and locator.is_visible():
                        return key, locator
                except Exception as exc:  # noqa: BLE001 - 다음 후보를 시도한다
                    last_error = type(exc).__name__
                    continue
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(250)  # 로딩 대기(사람 흉내가 아니라 렌더 대기)
        logger.warning(
            "selector 미일치 stage=%s keys=%s", stage.value, ",".join(keys_of(tuple(entries)))
        )
        self.capture_debug(f"selector_{stage.short}")
        raise SelectorMismatch(stage.value, keys_of(tuple(entries)), last_error)

    def _text_of(self, stage: SelectorStage, entries: Sequence[tuple[str, str]]) -> str:
        """첫 번째로 읽히는 텍스트를 돌려준다. 없으면 빈 문자열(추측하지 않는다)."""
        page = self._require_page()
        for key, selector in entries:
            locator = page.locator(selector).first
            try:
                if not locator.count():
                    continue
                value = locator.inner_text(timeout=self.selector_timeout_ms)
            except Exception as exc:  # noqa: BLE001 - selector 하나 실패가 전체를 막지 않는다
                logger.debug("텍스트 읽기 실패 stage=%s key=%s (%s)", stage.name, key, type(exc).__name__)
                continue
            if value and value.strip():
                return value.strip()
        return ""

    def _attr_of(
        self, stage: SelectorStage, entries: Sequence[tuple[str, str]], attribute: str
    ) -> str:
        page = self._require_page()
        for key, selector in entries:
            locator = page.locator(selector).first
            try:
                if not locator.count():
                    continue
                value = locator.get_attribute(attribute, timeout=self.selector_timeout_ms)
            except Exception as exc:  # noqa: BLE001
                logger.debug("속성 읽기 실패 stage=%s key=%s (%s)", stage.name, key, type(exc).__name__)
                continue
            if value:
                return str(value).strip()
        return ""

    def _hrefs(self, entries: Sequence[tuple[str, str]]) -> list[str]:
        """현재 화면에서 href를 모은다(요소가 없으면 빈 목록 — 대기하지 않는다)."""
        page = self._require_page()
        found: list[str] = []
        for _key, selector in entries:
            try:
                values = page.locator(selector).evaluate_all(
                    "nodes => nodes.map(n => n.getAttribute('href'))"
                )
            except Exception:  # noqa: BLE001 - 다음 selector를 시도한다
                continue
            for href in values or []:
                if not href:
                    continue
                url = href if href.startswith("http") else BASE_URL + href
                if url not in found:
                    found.append(url)
        return found

    # --- 읽기 ------------------------------------------------------------
    def session_state(self) -> SessionState:
        page = self._require_page()
        if not page.url.startswith(BASE_URL):
            self.goto(HOME_URL)
        body = (page.inner_text("body") or "").lower()

        if any(marker in body for marker in CHALLENGE_TEXTS):
            return SessionState.CHALLENGE
        if any(marker in body for marker in WARNING_TEXTS):
            return SessionState.PLATFORM_WARNING
        for selector in LOGIN_REQUIRED_MARKERS:
            if page.locator(selector).count():
                return SessionState.LOGIN_REQUIRED
        if any(marker in body for marker in LOGIN_TEXTS):
            return SessionState.LOGIN_REQUIRED
        for selector in LOGGED_IN_MARKERS:
            if page.locator(selector).count():
                return SessionState.LOGGED_IN
        return SessionState.UNKNOWN

    def search(self, query: str) -> None:
        """공개 Web UI를 클릭·입력으로 따라간다(비공개 endpoint를 추측하지 않는다)."""
        page = self._require_page()
        self.goto(HOME_URL)

        key, entry = self._locate(SelectorStage.SEARCH_ENTRY, SEARCH_ENTRY)
        logger.debug("검색 진입 selector=%s", key)
        entry.click(timeout=self.selector_timeout_ms)

        key, box = self._locate(SelectorStage.SEARCH_INPUT, SEARCH_INPUT)
        logger.debug("검색 입력 selector=%s", key)
        box.click(timeout=self.selector_timeout_ms)
        box.fill(query, timeout=self.selector_timeout_ms)

        # 결과 목록에서 공개 결과 페이지로 한 단계만 들어간다(과도한 depth 탐색 금지).
        target = None
        try:
            key, target = self._locate(SelectorStage.SEARCH_RESULT, SEARCH_RESULT_HASHTAG)
        except SelectorMismatch:
            key, target = self._locate(SelectorStage.SEARCH_RESULT, SEARCH_RESULT_ACCOUNT)
        logger.debug("검색 결과 selector=%s", key)
        target.click(timeout=self.selector_timeout_ms)

        page.wait_for_load_state("domcontentloaded")
        key, _marker = self._locate(SelectorStage.RESULT_PAGE, RESULT_PAGE_MARKERS)
        logger.debug("결과 페이지 selector=%s", key)

    def collect_post_links(self, limit: int) -> list[str]:
        page = self._require_page()
        links: list[str] = []
        for _ in range(max(1, self.scroll_rounds)):
            for url in self._hrefs(POST_LINK_SELECTORS):
                if url not in links:
                    links.append(url)
            if len(links) >= limit:
                return links[:limit]
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(min(self.selector_timeout_ms, 2000))
        if not links:
            self.capture_debug(f"selector_{SelectorStage.REEL_LINK.short}")
            raise SelectorMismatch(SelectorStage.REEL_LINK.value, keys_of(POST_LINK_SELECTORS))
        return links[:limit]

    def open_post(self, url: str) -> Optional[PostDetail]:
        self.goto(url)
        username = self._attr_of(SelectorStage.USERNAME, USERNAME_SELECTORS, "href")
        caption = self._text_of(SelectorStage.CAPTION, CAPTION_SELECTORS)
        if not username and not caption:
            # 화면 자체를 읽지 못했다 — 빈 값으로 추측해 저장하지 않는다.
            self.capture_debug(f"selector_{SelectorStage.POST_DETAIL.short}")
            return None
        return PostDetail(
            permalink=url,
            username=(username or "").strip("/").split("/")[0],
            caption=caption or "",
            hashtags=extract_hashtags(caption or ""),
            media_type="REEL" if "/reel/" in url else "POST",
        )

    def capture_debug(self, name: str) -> Optional[str]:
        """selector 실패 등 예외 상황에서만 화면을 저장한다(전체 HTML dump 금지)."""
        if self.debug_dir is None or self._screenshots >= self.max_screenshots:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            self._require_page().screenshot(path=str(path))
            self._screenshots += 1
            logger.info("디버그 스크린샷 저장: %s", path)
            return str(path)
        except Exception:  # noqa: BLE001 - 디버그 저장 실패가 Run을 막지 않는다
            return None

    def close(self) -> None:
        for closer in (self._context, self._playwright):
            try:
                if closer is not None:
                    closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:  # noqa: BLE001 - 종료 실패는 무시
                pass
        self._context = self._page = self._playwright = None

    def _require_page(self) -> Any:
        if self._page is None:
            raise DiscoveryError("브라우저가 시작되지 않았습니다(start() 호출 필요).")
        return self._page


# --- Discovery 소스 ---------------------------------------------------------
class InstagramBrowserDiscovery(DiscoverySource):
    """검색어로 Reel 후보를 찾아 RawCandidate로 만든다(저장은 호출자가 한다)."""

    name = SOURCE_BROWSER_SEARCH

    def __init__(
        self,
        browser: BrowserPage,
        queries: Sequence[str],
        *,
        max_queries_per_run: int = 5,
        max_candidates_per_query: int = 10,
        max_candidates_per_run: int = 40,
        media_types: Sequence[str] = ("REEL",),
        stop_on_login_required: bool = True,
        stop_on_challenge: bool = True,
        stop_on_warning: bool = True,
    ) -> None:
        self.browser = browser
        self.queries = [str(q).strip() for q in queries if str(q).strip()]
        self.max_queries_per_run = int(max_queries_per_run)
        self.max_candidates_per_query = int(max_candidates_per_query)
        self.max_candidates_per_run = int(max_candidates_per_run)
        self.media_types = {str(t).upper() for t in media_types}
        self.stop_on = {
            SessionState.LOGIN_REQUIRED: stop_on_login_required,
            SessionState.CHALLENGE: stop_on_challenge,
            SessionState.PLATFORM_WARNING: stop_on_warning,
        }
        self.stats = DiscoveryStats()

    def available(self) -> bool:
        return bool(self.queries)

    def check_session(self) -> SessionState:
        """세션 상태를 확인하고, 중단 조건이면 예외로 Run을 멈춘다."""
        state = self.browser.session_state()
        self.stats.session_state = state
        if state.can_continue:
            return state
        if self.stop_on.get(state, True):
            self.browser.capture_debug(f"session_{state.value.lower()}")
            raise BrowserSessionError(state.value, self._session_message(state))
        return state

    @staticmethod
    def _session_message(state: SessionState) -> str:
        if state is SessionState.LOGIN_REQUIRED:
            return (
                "Instagram 로그인이 필요합니다. run_instagram_session.bat 을 실행해 "
                "직접 로그인한 뒤 다시 시도하세요."
            )
        if state is SessionState.CHALLENGE:
            return "Instagram 본인 확인(Challenge)이 감지되어 중단합니다. 브라우저에서 직접 처리하세요."
        if state is SessionState.PLATFORM_WARNING:
            return "Instagram 이용 제한 경고가 감지되어 중단합니다. 당분간 실행하지 마세요."
        return "세션 상태를 확인할 수 없어 중단합니다."

    def discover(self, limit: Optional[int] = None) -> list[RawCandidate]:
        """검색어를 돌며 후보를 모은다. 검색어 하나의 실패는 격리한다."""
        self.check_session()

        budget = min(self.max_candidates_per_run, int(limit)) if limit else self.max_candidates_per_run
        candidates: list[RawCandidate] = []
        seen: set[str] = set()

        for query in self.queries[: self.max_queries_per_run]:
            if len(candidates) >= budget:
                break
            result = QueryResult(query=query)
            try:
                self.browser.search(query)
                links = self.browser.collect_post_links(self.max_candidates_per_query)
                result.found = len(links)
                for link in links:
                    if len(candidates) >= budget:
                        break
                    candidate = self._collect_one(link, query, seen, result)
                    if candidate is not None:
                        candidates.append(candidate)
                        result.collected += 1
            except BrowserSessionError:
                self.stats.add_query(result)
                self.stats.stop_reason = self.stats.session_state.value
                raise
            except SelectorMismatch as exc:
                self._record_failure(result, query, exc.stage, exc, exc.tried)
            except Exception as exc:  # noqa: BLE001 - 검색어 하나 실패가 Run을 멈추지 않는다
                self._record_failure(result, query, SelectorStage.SEARCH_RESULT.value, exc, ())
                self.browser.capture_debug(f"query_{query[:20]}")
            self.stats.add_query(result)

        logger.info(
            "Browser Discovery: 검색어 %d개(성공 %d / 실패 %d) · 발견 %d · 수집 %d",
            len(self.stats.queries),
            self.stats.ok_queries,
            self.stats.failed_queries,
            self.stats.found,
            self.stats.collected,
        )
        return candidates

    @staticmethod
    def _record_failure(
        result: QueryResult,
        query: str,
        stage: str,
        exc: BaseException,
        tried: Sequence[str],
    ) -> None:
        """실패를 'query / stage / selector key / 예외 타입'으로 남긴다(HTML dump 없음)."""
        keys = ",".join(tried) if tried else "-"
        message = f"stage={stage} keys={keys} type={type(exc).__name__}"
        result.errors.append(message)
        result.failed_stage = stage
        logger.warning("검색어 '%s' 실패: %s", query, message)

    def _collect_one(
        self, link: str, query: str, seen: set[str], result: QueryResult
    ) -> Optional[RawCandidate]:
        try:
            normalized = normalize_instagram_url(link)
        except DiscoveryError:
            return None
        if normalized.media_type not in self.media_types:
            return None
        if normalized.canonical_url in seen:
            return None
        seen.add(normalized.canonical_url)

        detail = self.browser.open_post(normalized.canonical_url)
        if detail is None:
            # 상세를 읽지 못한 건은 후보로 만들지 않는다(검색어 전체를 실패시키지도 않는다).
            message = (
                f"stage={SelectorStage.POST_DETAIL.value} "
                f"shortcode={normalized.shortcode} type=EmptyDetail"
            )
            result.errors.append(message)
            result.failed_stage = SelectorStage.POST_DETAIL.value
            logger.warning("게시물 상세 읽기 실패: %s", message)
            self.browser.capture_debug(f"selector_{SelectorStage.POST_DETAIL.short}")
            return None

        # URL 정규화와 후보 생성은 Phase 12A 구현을 그대로 쓴다.
        candidate = candidate_from_url(
            normalized.canonical_url,
            username=detail.username or None,
            caption=detail.caption,
            note=f"browser search: {query}",
            source=SOURCE_BROWSER_SEARCH,
        )
        candidate.like_count = detail.like_count
        candidate.comment_count = detail.comment_count
        candidate.extra["source_query"] = query
        return candidate
