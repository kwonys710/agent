"""BrowserExecutor — v0.1 Stub(의도적으로 미구현).

구현 원칙(요구사항 4항):
- 탐지 우회 / CAPTCHA 우회 / Challenge 우회 / Rate Limit 우회 기능을 만들지 않는다.
- 자동화를 사람 행동처럼 위장하지 않는다.
- 계정 ID/Password를 코드나 SQLite에 평문 저장하지 않는다.
- 플랫폼 Warning, 인증 요구가 감지되면 즉시 중단한다.

위 원칙을 지키는 범위에서만(=운영자가 직접 로그인한 브라우저 세션을 사용하는 방식 등)
Phase 9에서 필요성을 재검토한 뒤 구현한다. 그 전까지 이 Executor는 실행을 거부한다.
"""
from __future__ import annotations

from ...core.logger import get_logger
from ...core.models import ExecutionResult, QueuedAction
from .base import BaseExecutor

logger = get_logger("actions.executor.browser")

NOT_IMPLEMENTED = (
    "BrowserExecutor는 v0.1에서 구현되지 않았습니다(Phase 9 검토 대상). "
    "executor.mode를 manual로 설정하세요."
)


class BrowserExecutor(BaseExecutor):
    """브라우저 자동화 Executor 자리 표시자. 어떤 Action도 실행하지 않는다."""

    name = "browser"
    supports_like = False
    supports_comment = False

    def validate_session(self) -> bool:
        logger.warning(NOT_IMPLEMENTED)
        return False

    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        return self._skipped("browser_executor_not_implemented")

    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        return self._skipped("browser_executor_not_implemented")

    def health_check(self) -> dict[str, object]:
        info = super().health_check()
        info["note"] = NOT_IMPLEMENTED
        return info
