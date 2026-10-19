"""Executor 인터페이스.

실제 좋아요/댓글 수행 방식(수동/공식 API/브라우저)을 파이프라인에서 완전히 분리한다.
Action Controller는 이 인터페이스만 사용하므로 Executor 교체가 가능하다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ...core.models import ActionStatus, ExecutionResult, QueuedAction


class BaseExecutor(ABC):
    """모든 Executor의 공통 계약."""

    name: str = "base"
    supports_like: bool = False
    supports_comment: bool = False

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    @abstractmethod
    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        """좋아요를 수행한다(또는 수행 지시를 생성한다)."""

    @abstractmethod
    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        """댓글을 작성한다(또는 작성 지시를 생성한다)."""

    def validate_session(self) -> bool:
        """실행 가능한 세션/인증 상태인지 확인한다."""
        return True

    def health_check(self) -> dict[str, object]:
        """Executor 상태 요약(로그/Summary용)."""
        return {
            "executor": self.name,
            "dry_run": self.dry_run,
            "supports_like": self.supports_like,
            "supports_comment": self.supports_comment,
        }

    def finalize(self) -> None:
        """실행 종료 시 정리 작업(파일 flush 등). 기본은 아무 것도 하지 않는다."""

    # --- 공통 헬퍼 -------------------------------------------------------
    def _skipped(self, detail: str) -> ExecutionResult:
        return ExecutionResult(
            success=False, status=ActionStatus.SKIPPED, detail=detail, dry_run=self.dry_run
        )

    def _success(self, detail: str) -> ExecutionResult:
        return ExecutionResult(
            success=True, status=ActionStatus.SUCCESS, detail=detail, dry_run=self.dry_run
        )

    def _failed(self, error: str) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            status=ActionStatus.FAILED,
            detail="",
            error=error,
            dry_run=self.dry_run,
        )
