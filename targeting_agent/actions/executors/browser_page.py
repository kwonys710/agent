"""Action 실행용 브라우저 페이지 (Phase 18B).

운영자가 직접 로그인한 persistent profile을 재사용해 **게시물 화면에서 좋아요/댓글만**
수행한다. Discovery(읽기) 모듈과 분리해 두어, 읽기 경로에 쓰기 코드가 섞이지 않게 한다.

하지 않는 것(구현 자체를 두지 않는다):
- 팔로우 / DM / 저장 / 공유
- CAPTCHA·Challenge·경고·Rate Limit 우회, stealth/fingerprint/proxy/계정 rotation
- 비공식 API·GraphQL 호출, network 응답 가로채기
- ID/PW 자동 입력, 쿠키·세션 토큰 추출
- 사람처럼 보이기 위한 랜덤 지연(렌더 대기만 사용)

로그인 필요 / Challenge / 경고 / 작업 차단이 감지되면 **즉시 중단**한다(재시도 없음).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from ...core.exceptions import ExecutorError
from ...core.logger import get_logger
from ...discovery.browser_models import SessionState
from ...discovery.browser_selectors import (
    CHALLENGE_TEXTS,
    HOME_URL,
    LOGGED_IN_MARKERS,
    LOGIN_REQUIRED_MARKERS,
    LOGIN_TEXTS,
    WARNING_TEXTS,
)
from .action_selectors import (
    ALREADY_LIKED,
    COMMENT_INPUT,
    COMMENT_SUBMIT,
    LIKE_BUTTON,
    ACTION_BLOCKED_TEXTS,
    CommentVerify,
    keys_of,
)

logger = get_logger("actions.executor.browser_page")

# 중단 사유(ACTION_BLOCKED는 세션 문제가 아니라 '지금 이 동작이 막힘'이다)
STOP_ACTION_BLOCKED = "ACTION_BLOCKED"

# 댓글 게시 결과(Phase 19G). "입력란이 비었다"는 약한 근거를 쓰지 않는다.
COMMENT_CONFIRMED = "COMMENT_CONFIRMED"  # 목록에 보이고 reload 후에도 남아 있다
COMMENT_NOT_SENT = "COMMENT_NOT_SENT"  # 입력란/게시 버튼이 없어 아무것도 올리지 않았다
COMMENT_UNKNOWN_STATE = "COMMENT_UNKNOWN_STATE"  # 눌렀지만 확인 불가(재시도 금지)


def normalize_comment_text(text: str) -> str:
    """공백 차이는 무시한다(줄바꿈·연속 공백으로 DOM 표기가 달라지기 때문)."""
    return " ".join((text or "").split())


@dataclass(frozen=True)
class CommentResult:
    """댓글 게시 결과. state는 위 세 상수 중 하나, text는 **확인된** 댓글 문구다."""

    state: str
    text: str = ""
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        return self.state == COMMENT_CONFIRMED


class ActionPage(Protocol):
    """Executor가 사용하는 브라우저 동작(LIKE/COMMENT만)."""

    def ensure_logged_in(self) -> SessionState: ...

    def open_media(self, url: str) -> None: ...

    def stop_reason(self) -> Optional[str]: ...

    def already_liked(self) -> bool: ...

    def like(self) -> bool: ...

    def comment(self, text: str) -> bool: ...

    def close(self) -> None: ...


@dataclass
class PlaywrightActionPage:
    """Playwright 구현. playwright는 지연 import한다."""

    profile_dir: Path
    headless: bool = False
    timeout_ms: int = 20000
    selector_timeout_ms: int = 4000
    executable_path: Optional[str] = None
    home_url: str = HOME_URL
    _playwright: Any = None
    _context: Any = None
    _page: Any = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 설치 여부에 따른 분기
            raise ExecutorError(
                "playwright가 설치되어 있지 않습니다. "
                "pip install playwright && python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        options: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
        }
        if self.executable_path:
            options["executable_path"] = self.executable_path
        self._context = self._playwright.chromium.launch_persistent_context(**options)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    # --- 공통 ------------------------------------------------------------
    def _require_page(self) -> Any:
        if self._page is None:
            raise ExecutorError("브라우저가 시작되지 않았습니다(start() 호출 필요).")
        return self._page

    def _find(self, entries: Sequence[tuple[str, str]], *, wait: bool = True) -> Optional[Any]:
        """단계 하나에 대기 예산을 한 번만 쓴다(Discovery와 같은 전략)."""
        page = self._require_page()
        deadline = time.monotonic() + (max(self.selector_timeout_ms, 200) / 1000 if wait else 0)
        while True:
            for key, selector in entries:
                locator = page.locator(selector).first
                try:
                    if locator.count() and locator.is_visible():
                        logger.debug("요소 발견 key=%s", key)
                        return locator
                except Exception:  # noqa: BLE001 - 다음 후보 시도
                    continue
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(200)

    def _body_text(self) -> str:
        try:
            return (self._require_page().inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    # --- 상태 ------------------------------------------------------------
    def session_state(self) -> SessionState:
        page = self._require_page()
        body = self._body_text()
        if any(text in body for text in CHALLENGE_TEXTS):
            return SessionState.CHALLENGE
        if any(text in body for text in WARNING_TEXTS):
            return SessionState.PLATFORM_WARNING
        for selector in LOGIN_REQUIRED_MARKERS:
            if page.locator(selector).count():
                return SessionState.LOGIN_REQUIRED
        if any(text in body for text in LOGIN_TEXTS):
            return SessionState.LOGIN_REQUIRED
        for selector in LOGGED_IN_MARKERS:
            if page.locator(selector).count():
                return SessionState.LOGGED_IN
        return SessionState.UNKNOWN

    def ensure_logged_in(self) -> SessionState:
        """홈 화면을 열어 로그인 상태를 확인한다(Action Run 시작 전 1회)."""
        page = self._require_page()
        page.goto(self.home_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        return self.session_state()

    def stop_reason(self) -> Optional[str]:
        """Action을 멈춰야 하는 **명확한 근거**가 보일 때만 이유를 돌려준다.

        상태를 알 수 없는 것(UNKNOWN)은 중단 근거가 아니다 — 화면 판정 실패 하나로
        승인된 Action 전체가 멈추면 운영이 불가능해진다. 중단은 아래 신호에만 반응한다.
        """
        body = self._body_text()
        if any(text in body for text in ACTION_BLOCKED_TEXTS):
            return STOP_ACTION_BLOCKED
        if any(text in body for text in CHALLENGE_TEXTS):
            return SessionState.CHALLENGE.value
        if any(text in body for text in WARNING_TEXTS):
            return SessionState.PLATFORM_WARNING.value
        page = self._require_page()
        for selector in LOGIN_REQUIRED_MARKERS:
            try:
                if page.locator(selector).count():
                    return SessionState.LOGIN_REQUIRED.value
            except Exception:  # noqa: BLE001
                continue
        if any(text in body for text in LOGIN_TEXTS):
            return SessionState.LOGIN_REQUIRED.value
        return None

    # --- 동작 ------------------------------------------------------------
    def open_media(self, url: str) -> None:
        page = self._require_page()
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

    def already_liked(self) -> bool:
        return self._find(ALREADY_LIKED, wait=False) is not None

    def like(self) -> bool:
        """좋아요를 누르고 상태가 바뀐 것을 확인한다. 실패하면 재시도하지 않는다."""
        button = self._find(LIKE_BUTTON)
        if button is None:
            logger.warning("좋아요 버튼을 찾지 못했습니다(keys=%s)", ",".join(keys_of(LIKE_BUTTON)))
            return False
        button.click(timeout=self.selector_timeout_ms)
        self._require_page().wait_for_timeout(min(self.selector_timeout_ms, 1500))
        return self.already_liked()

    def _find_own_comment(self, text: str) -> Optional[str]:
        """댓글 목록에서 내가 쓴 문구를 찾는다(공백 정규화 후 '포함' 비교).

        DOM 전체가 일치할 필요는 없다 — 실제 UI는 항목 안에 작성자/시간이 함께 들어간다.
        단계 하나당 대기 예산(selector_timeout_ms)을 한 번만 쓴다.
        """
        page = self._require_page()
        needle = normalize_comment_text(text)
        if not needle:
            return None
        deadline = time.monotonic() + max(self.selector_timeout_ms, 200) / 1000
        while True:
            for key, selector in CommentVerify.ITEMS:
                try:
                    texts = page.locator(selector).all_inner_texts()
                except Exception:  # noqa: BLE001 - 다음 후보 시도
                    continue
                for raw in texts:
                    normalized = normalize_comment_text(raw)
                    if needle in normalized:
                        logger.debug("내 댓글 확인 key=%s", key)
                        return normalized
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(200)

    def post_comment(self, text: str) -> CommentResult:
        """댓글을 올리고 **목록에 보이는지 + reload 후에도 남아 있는지**로 확인한다.

        게시 버튼은 단 한 번만 누른다. 확인에 실패하면 UNKNOWN_STATE로 남기고
        절대 다시 올리지 않는다 — 공개된 중복 댓글이 '알 수 없음'보다 나쁘다.
        """
        page = self._require_page()
        box = self._find(COMMENT_INPUT)
        if box is None:
            logger.warning("댓글 입력란을 찾지 못했습니다(keys=%s)", ",".join(keys_of(COMMENT_INPUT)))
            return CommentResult(COMMENT_NOT_SENT, detail="input_not_found")
        box.click(timeout=self.selector_timeout_ms)
        box.fill(text, timeout=self.selector_timeout_ms)

        submit = self._find(COMMENT_SUBMIT)
        if submit is None:
            logger.warning("댓글 게시 버튼을 찾지 못했습니다(keys=%s)", ",".join(keys_of(COMMENT_SUBMIT)))
            return CommentResult(COMMENT_NOT_SENT, detail="submit_not_found")
        submit.click(timeout=self.selector_timeout_ms)  # 재시도 없음(클릭 1회)

        if self._find_own_comment(text) is None:
            logger.warning("댓글을 게시했지만 목록에서 확인하지 못했습니다(재시도하지 않음)")
            return CommentResult(COMMENT_UNKNOWN_STATE, detail="not_in_comment_list")

        # reload 후에도 보이면 서버에 실제로 남은 것으로 본다(낙관적 UI 구분).
        try:
            page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001 - 새로고침 실패는 확인 불가로 처리
            logger.warning("댓글 확인용 새로고침에 실패했습니다: %s", exc)
            return CommentResult(COMMENT_UNKNOWN_STATE, detail="reload_failed")
        persisted = self._find_own_comment(text)
        if persisted is None:
            logger.warning("새로고침 후 댓글이 남아 있지 않습니다(재시도하지 않음)")
            return CommentResult(COMMENT_UNKNOWN_STATE, detail="not_persisted")
        return CommentResult(COMMENT_CONFIRMED, text=persisted)

    def comment(self, text: str) -> bool:
        """기존 Executor 호환용 얇은 래퍼. 확인된 경우에만 True다."""
        return self.post_comment(text).confirmed

    def close(self) -> None:
        for closer in (self._context, self._playwright):
            try:
                if closer is not None:
                    closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:  # noqa: BLE001
                pass
        self._context = self._page = self._playwright = None
