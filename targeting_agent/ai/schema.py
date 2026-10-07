"""Runtime Claude 응답 스키마와 검증 (Phase 13).

Claude Code CLI의 `--output-format json`은 **CLI 결과 wrapper**이고,
그 안의 `result` 문자열이 모델이 생성한 응답이다. 둘은 별개이므로
wrapper 성공(`is_error: false`)만 보고 결과를 신뢰하지 않는다.
실제 payload는 여기 정의한 스키마로 검증한 뒤에만 사용한다.
"""
from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

MAX_TOPICS = 6
MAX_COMMENTS = 5


class FailureReason(str, Enum):
    """Claude 결과를 쓸 수 없을 때의 사유(로그·통계용)."""

    CLAUDE_UNAVAILABLE = "CLAUDE_UNAVAILABLE"
    INVALID_JSON = "INVALID_JSON"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    TIMEOUT = "TIMEOUT"
    USAGE_LIMIT = "USAGE_LIMIT"
    CLI_ERROR = "CLI_ERROR"
    DISABLED = "DISABLED"
    PREFILTERED = "PREFILTERED"


class TargetingAnalysis(BaseModel):
    """한 후보에 대한 Claude 분석 결과."""

    summary: str = ""
    primary_topic: str = ""
    topics: list[str] = Field(default_factory=list)
    mood: str = ""
    language: str = "unknown"
    relevance_score: float = 0.0
    relevance_reason: str = ""
    comment_candidates: list[str] = Field(default_factory=list)

    @field_validator("relevance_score")
    @classmethod
    def _score_in_range(cls, value: float) -> float:
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("relevance_score는 0.0~1.0이어야 합니다.")
        return float(value)

    @field_validator("topics", "comment_candidates", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("문자열 배열이어야 합니다.")
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("topics")
    @classmethod
    def _limit_topics(cls, value: list[str]) -> list[str]:
        return value[:MAX_TOPICS]

    @field_validator("comment_candidates")
    @classmethod
    def _limit_comments(cls, value: list[str]) -> list[str]:
        return value[:MAX_COMMENTS]

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"ko", "en", "mixed", "unknown"} else "unknown"


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """모델 응답 텍스트에서 JSON 객체 하나를 뽑는다(코드펜스/잡음 허용)."""
    if not text:
        return None
    candidates: list[str] = []
    fence = FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for chunk in candidates:
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def parse_analysis(text: str) -> tuple[Optional[TargetingAnalysis], Optional[FailureReason], str]:
    """응답 텍스트 → 검증된 분석 결과. 실패 시 사유와 메시지를 돌려준다."""
    payload = extract_json_object(text)
    if payload is None:
        return None, FailureReason.INVALID_JSON, (text or "")[:200]
    try:
        return TargetingAnalysis.model_validate(payload), None, ""
    except ValidationError as exc:
        return None, FailureReason.INVALID_SCHEMA, str(exc)[:300]
