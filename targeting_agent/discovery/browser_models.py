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
