"""Targeting Agent 공통 예외."""
from __future__ import annotations


class TargetingError(RuntimeError):
    """모든 Targeting Agent 예외의 최상위 타입."""


class ConfigError(TargetingError):
    """config.yaml 누락/형식 오류/필수값 누락."""


class DatabaseError(TargetingError):
    """SQLite 초기화 또는 쓰기 실패."""


class DiscoveryError(TargetingError):
    """후보 수집 실패(파일 없음, 포맷 오류, API 미지원 등)."""


class AnalysisError(TargetingError):
    """콘텐츠 분석 실패."""


class CommentGenerationError(TargetingError):
    """댓글 생성 실패(품질/중복 필터를 통과한 후보가 없음 등)."""


class ExecutorError(TargetingError):
    """Executor 실행 실패."""


class RateLimitExceeded(TargetingError):
    """내부 Daily Limit / Creator Cooldown 초과. 플랫폼 제한 우회 목적이 아니다."""


class PlatformWarningError(TargetingError):
    """플랫폼 Warning / Challenge / 로그인 요구 감지. 즉시 자동 실행을 중단한다."""
