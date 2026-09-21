"""도메인 모델과 상태 Enum.

SQLite 행(sqlite3.Row)과 파이프라인 단계 사이에서 주고받는 값 객체들.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class MediaStatus(str, Enum):
    NEW = "NEW"
    # 입력은 됐지만 분석에 필요한 정보(caption/hashtag)가 없어 보강이 필요한 상태(Phase 12A)
    NEEDS_ENRICHMENT = "NEEDS_ENRICHMENT"
    ANALYZED = "ANALYZED"
    SCORED = "SCORED"
    QUEUED = "QUEUED"
    INTERACTED = "INTERACTED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class ActionType(str, Enum):
    LIKE = "LIKE"
    COMMENT = "COMMENT"


class ActionStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class DraftStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"


class FeedbackType(str, Enum):
    GOOD_TARGET = "GOOD_TARGET"
    BAD_TARGET = "BAD_TARGET"
    NOT_MY_STYLE = "NOT_MY_STYLE"
    GOOD_COMMENT = "GOOD_COMMENT"
    BAD_COMMENT = "BAD_COMMENT"
    LIKED = "LIKED"
    COMMENTED = "COMMENTED"
    RESPONDED = "RESPONDED"
    FOLLOWED = "FOLLOWED"


@dataclass
class RawCandidate:
    """Discovery가 수집한 원본 후보(정규화 전)."""

    media_id: str
    permalink: str
    username: str
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    media_type: str = "REEL"
    like_count: int = 0
    comment_count: int = 0
    posted_at: Optional[str] = None
    language: Optional[str] = None
    followers: int = 0
    is_private: bool = False
    is_ad: bool = False
    source: str = "import"
    # Phase 12A: URL 입력 경로에서 사용. 모르는 값은 추측하지 않고 None으로 둔다.
    canonical_url: Optional[str] = None
    instagram_media_id: Optional[str] = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContentAnalysis:
    """콘텐츠 분석 결과(캐시 대상)."""

    topics: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    language: str = "unknown"
    tone: str = "neutral"
    summary: str = ""
    is_ad: bool = False
    is_sensitive: bool = False
    analyzer: str = "heuristic"
    analyzer_version: str = "1"
    prompt_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "topics": self.topics,
            "keywords": self.keywords,
            "language": self.language,
            "tone": self.tone,
            "summary": self.summary,
            "is_ad": self.is_ad,
            "is_sensitive": self.is_sensitive,
            "analyzer": self.analyzer,
            "analyzer_version": self.analyzer_version,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ContentAnalysis":
        return cls(
            topics=list(data.get("topics") or []),
            keywords=list(data.get("keywords") or []),
            language=str(data.get("language") or "unknown"),
            tone=str(data.get("tone") or "neutral"),
            summary=str(data.get("summary") or ""),
            is_ad=bool(data.get("is_ad")),
            is_sensitive=bool(data.get("is_sensitive")),
            analyzer=str(data.get("analyzer") or "heuristic"),
            analyzer_version=str(data.get("analyzer_version") or "1"),
            prompt_version=str(data.get("prompt_version") or "1"),
        )


@dataclass
class ScoreBreakdown:
    """Target Score 구성요소(각 0~1) 및 최종 점수(0~100)."""

    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": self.components,
            "weights": self.weights,
            "total": self.total,
            "notes": self.notes,
        }


@dataclass
class CommentCandidate:
    """생성된 댓글 후보."""

    text: str
    generator: str = "template"
    generator_version: str = "1"
    quality_ok: bool = True
    quality_reason: str = ""
    similarity_max: float = 0.0
    status: DraftStatus = DraftStatus.CANDIDATE
    draft_id: Optional[int] = None


@dataclass
class QueuedAction:
    """Action Queue 한 건."""

    action_id: int
    media_pk: int
    media_id: str
    permalink: str
    username: str
    creator_id: int
    action_type: ActionType
    target_score: float
    priority: int
    status: ActionStatus = ActionStatus.PENDING
    comment_text: Optional[str] = None
    draft_id: Optional[int] = None
    executor: str = "manual"


@dataclass
class ExecutionResult:
    """Executor 실행 결과."""

    success: bool
    status: ActionStatus
    detail: str = ""
    error: Optional[str] = None
    dry_run: bool = True
