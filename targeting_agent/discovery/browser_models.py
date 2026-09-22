"""Browser Discovery 값 객체 (Phase 18A).

공개 화면에서 읽은 것만 담는다. 화면에 없는 값은 추측하지 않고 비워 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

SOURCE_BROWSER_SEARCH = "instagram_browser_search"


class SessionState(str, Enum):
    """로그인/세션 상태. LOGGED_IN이 아니면 Discovery를 시작하지 않는다."""

    LOGGED_IN = "LOGGED_IN"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CHALLENGE = "CHALLENGE"
    PLATFORM_WARNING = "PLATFORM_WARNING"
    UNKNOWN = "UNKNOWN"

    @property
    def can_continue(self) -> bool:
        return self is SessionState.LOGGED_IN


# Run을 즉시 중단해야 하는 상태
STOP_STATES = (
    SessionState.LOGIN_REQUIRED,
    SessionState.CHALLENGE,
    SessionState.PLATFORM_WARNING,
)


class SelectorStage(str, Enum):
    """검색 흐름의 단계. 실패했을 때 "어디서" 막혔는지 그대로 보고한다(Phase 18A.1)."""

    SEARCH_ENTRY = "SEARCH_ENTRY_NOT_FOUND"
    SEARCH_INPUT = "SEARCH_INPUT_NOT_FOUND"
    SEARCH_RESULT = "SEARCH_RESULT_NOT_FOUND"
    RESULT_PAGE = "RESULT_PAGE_NOT_FOUND"
    REEL_LINK = "REEL_LINK_NOT_FOUND"
    POST_DETAIL = "POST_DETAIL_NOT_FOUND"
    USERNAME = "USERNAME_NOT_FOUND"
    CAPTION = "CAPTION_NOT_FOUND"

    @property
    def short(self) -> str:
        """스크린샷 파일명에 쓸 짧은 이름."""
        return self.name.lower()


@dataclass
class PostDetail:
    """게시물 상세 화면에서 읽은 공개 정보."""

    permalink: str
    username: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    media_type: str = "REEL"
    like_count: int = 0
    comment_count: int = 0

    @property
    def has_analyzable_text(self) -> bool:
        return bool(self.caption.strip() or self.hashtags)


@dataclass
class QueryResult:
    """검색어 1건의 처리 결과."""

    query: str
    found: int = 0
    collected: int = 0
    errors: list[str] = field(default_factory=list)
    failed_stage: Optional[str] = None

    @property
    def ok(self) -> bool:
        """검색 흐름이 끝까지 돈 검색어.

        후보를 하나라도 수집했으면 성공으로 본다(게시물 1건의 상세 실패는 검색어 실패가 아니다).
        수집이 0건인데 오류가 있으면 실패다 — 수집 0건 자체는 실패가 아니다.
        """
        return self.collected > 0 or not self.errors


@dataclass
class DiscoveryStats:
    """Discovery Run 집계."""

    session_state: SessionState = SessionState.UNKNOWN
    queries: list[QueryResult] = field(default_factory=list)
    found: int = 0
    collected: int = 0
    selector_errors: int = 0
    stop_reason: Optional[str] = None

    def add_query(self, result: QueryResult) -> None:
        self.queries.append(result)
        self.found += result.found
        self.collected += result.collected
        self.selector_errors += len(result.errors)

    @property
    def ok_queries(self) -> int:
        return sum(1 for q in self.queries if q.ok)

    @property
    def failed_queries(self) -> int:
        return sum(1 for q in self.queries if not q.ok)
