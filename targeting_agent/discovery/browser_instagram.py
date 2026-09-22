"""Instagram Browser Discovery (Phase 18A).

운영자가 **직접 로그인한** 브라우저 프로필을 재사용해 Instagram 웹 화면을 열고,
검색 결과에서 Reel 후보를 찾아 공개 DOM 정보만 읽는다.

하지 않는 것(구현 자체를 두지 않는다):
- 좋아요 / 댓글 / 팔로우 / DM / 저장 / 공유 등 **모든 쓰기 동작**
- CAPTCHA·Challenge·경고·Rate Limit 우회, stealth/fingerprint/proxy/계정 rotation
- 비공식 API·GraphQL endpoint 호출, network 응답 가로채기
- ID/PW 자동 입력, 쿠키·세션 토큰 추출/저장
- 사람처럼 보이기 위한 랜덤 지연(로딩 대기만 사용)

로그인 필요/Challenge/경고가 감지되면 **즉시 중단**한다.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from ..core.exceptions import BrowserSessionError, DiscoveryError
from ..core.logger import get_logger
from ..core.models import RawCandidate
from .base import DiscoverySource, extract_hashtags
from .browser_models import (
    SOURCE_BROWSER_SEARCH,
    DiscoveryStats,
    PostDetail,
    QueryResult,
    SessionState,
)
from .browser_selectors import (
    BASE_URL,
    CAPTION_SELECTORS,
    CHALLENGE_TEXTS,
    LOGGED_IN_MARKERS,
    LOGIN_REQUIRED_MARKERS,
    LOGIN_TEXTS,
    POST_LINK_SELECTORS,
    SEARCH_URL,
    USERNAME_SELECTORS,
    WARNING_TEXTS,
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
    """

    profile_dir: Path
    headless: bool = False
    timeout_ms: int = 20000
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

    # --- 읽기 ----------------------------------------------------------
    def goto(self, url: str) -> None:
        self._require_page().goto(url, wait_until="domcontentloaded")
        self._require_page().wait_for_load_state("networkidle")

    def session_state(self) -> SessionState:
        page = self._require_page()
        if not page.url.startswith(BASE_URL):
            self.goto(BASE_URL + "/")
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
        from urllib.parse import quote

        self.goto(SEARCH_URL.format(query=quote(query)))

    def collect_post_links(self, limit: int) -> list[str]:
        page = self._require_page()
        links: list[str] = []
        for _ in range(max(1, self.scroll_rounds)):
            for selector in POST_LINK_SELECTORS:
                for href in page.locator(selector).evaluate_all(
                    "nodes => nodes.map(n => n.getAttribute('href'))"
                ):
                    if not href:
                        continue
                    url = href if href.startswith("http") else BASE_URL + href
                    if url not in links:
                        links.append(url)
                if len(links) >= limit:
                    return links[:limit]
            page.mouse.wheel(0, 2000)
            page.wait_for_load_state("networkidle")
        return links[:limit]

    def open_post(self, url: str) -> Optional[PostDetail]:
        self.goto(url)
        page = self._require_page()
        username = self._first_text(USERNAME_SELECTORS, attribute="href")
        caption = self._first_text(CAPTION_SELECTORS)
        if not username and not caption:
            return None
        return PostDetail(
            permalink=url,
            username=(username or "").strip("/").split("/")[0],
            caption=caption or "",
            hashtags=extract_hashtags(caption or ""),
            media_type="REEL" if "/reel/" in url else "POST",
        )

    def _first_text(self, selectors: Sequence[str], attribute: Optional[str] = None) -> str:
        page = self._require_page()
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if not locator.count():
                    continue
                value = (
                    locator.get_attribute(attribute) if attribute else locator.inner_text()
                )
            except Exception:  # noqa: BLE001 - selector 하나 실패가 전체를 막지 않는다
                continue
            if value:
                return str(value).strip()
        return ""

    def capture_debug(self, name: str) -> Optional[str]:
        """selector 실패 등 예외 상황에서만 화면을 저장한다(전체 HTML dump 금지)."""
        if self.debug_dir is None or self._screenshots >= self.max_screenshots:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.png"
            self._require_page().screenshot(path=str(path))
            self._screenshots += 1
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
            except Exception as exc:  # noqa: BLE001 - 검색어 하나 실패가 Run을 멈추지 않는다
                message = f"SELECTOR_MISMATCH {type(exc).__name__}: {str(exc)[:80]}"
                logger.warning("검색어 '%s' 처리 실패: %s", query, message)
                result.errors.append(message)
                self.browser.capture_debug(f"query_{query[:20]}")
            self.stats.add_query(result)

        logger.info(
            "Browser Discovery: 검색어 %d개 · 발견 %d · 수집 %d (selector 오류 %d)",
            len(self.stats.queries),
            self.stats.found,
            self.stats.collected,
            self.stats.selector_errors,
        )
        return candidates

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
            result.errors.append(f"SELECTOR_MISMATCH detail:{normalized.shortcode}")
            self.browser.capture_debug(f"detail_{normalized.shortcode}")
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
