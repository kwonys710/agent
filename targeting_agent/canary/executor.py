"""LIKE 전용 Executor (Phase 18E).

Canary는 **기존 BrowserExecutor를 그대로 쓴다.** 별도의 click 경로를 만들지
않는다 — 그러면 기존 안전 검사를 우회하는 길이 하나 더 생긴다.

이 모듈이 더하는 것은 두 가지뿐이다.

1. COMMENT를 **코드 수준에서 막는다.**
   Queue에 승인된 COMMENT가 섞여 있어도, 어떤 경로로 들어와도 실행되지 않는다.
   호출 자체를 위반으로 보고 Run을 멈춘다 — 조용히 건너뛰면 "왜 안 나갔지"를
   나중에 알 수 없고, 반대로 조용히 나가면 더 나쁘다.

2. 눌린 것을 **새로고침 후에도** 확인한다.
   click()이 성공했다는 것과 좋아요가 실제로 남았다는 것은 다른 말이다.
   확인하지 못하면 UNKNOWN_WRITE_STATE로 남기고 다시 누르지 않는다 —
   다시 누르면 좋아요가 취소된다.
"""
from __future__ import annotations

from typing import Optional

from ..actions.executors.base import BaseExecutor
from ..actions.executors.browser import BrowserExecutor
from ..core.exceptions import PlatformWarningError
from ..core.logger import get_logger
from ..core.models import ActionStatus, ActionType, ExecutionResult, QueuedAction

logger = get_logger("canary.executor")

VERIFIED = "verified"
UNKNOWN_WRITE_STATE = "UNKNOWN_WRITE_STATE"


class CanaryViolation(PlatformWarningError):
    """Canary가 해서는 안 되는 일을 시도했다.

    Action 하나의 실패가 아니라 **Run 전체를 멈출 사유**다. 그래서
    PlatformWarningError를 상속한다 — Controller가 이미 가진 '즉시 중단' 경로를
    그대로 타고, 남은 Action은 실행되지 않는다. 일반 예외로 두면 Controller가
    Action 단위로 격리해 버려서 나머지가 계속 나간다.
    """


class LikeOnlyExecutor(BaseExecutor):
    """LIKE만 수행하고, 누른 결과를 새로고침해 확인하는 Executor."""

    name = "browser"  # 감사 기록에는 실제로 동작한 Executor 이름을 그대로 남긴다
    supports_like = True
    supports_comment = False

    def __init__(self, inner: BrowserExecutor) -> None:
        super().__init__(dry_run=inner.dry_run)
        self.inner = inner
        self.attempted = 0
        self.confirmed = 0
        self.unknown_write = 0
        self.comment_attempts = 0

    # --- 계약 -------------------------------------------------------------
    @property
    def live(self) -> bool:
        return bool(getattr(self.inner, "live", False))

    def validate_session(self) -> bool:
        return self.inner.validate_session()

    def finalize(self) -> None:
        self.inner.finalize()

    def health_check(self) -> dict[str, object]:
        info = self.inner.health_check()
        info.update(
            {
                "canary": True,
                "attempted": self.attempted,
                "confirmed": self.confirmed,
                "unknown_write": self.unknown_write,
            }
        )
        return info

    # --- COMMENT 차단 ------------------------------------------------------
    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        """절대 실행하지 않는다. 호출됐다는 사실 자체가 설계 위반이다."""
        self.comment_attempts += 1
        logger.error(
            "CRITICAL_CANARY_VIOLATION — Canary에서 COMMENT 실행이 시도되었습니다 "
            "(action_id=%s). Run을 중단합니다.",
            action.action_id,
        )
        raise CanaryViolation(
            f"CRITICAL_CANARY_VIOLATION: comment attempted on action {action.action_id}"
        )

    # --- LIKE --------------------------------------------------------------
    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        if action.action_type is not ActionType.LIKE:
            raise CanaryViolation(
                f"CRITICAL_CANARY_VIOLATION: non-LIKE action {action.action_type} reached executor"
            )
        self.attempted += 1
        result = self.inner.execute_like(action)

        if result.status is not ActionStatus.SUCCESS:
            return result
        if not self.live:
            return result  # DRY_RUN은 브라우저를 열지 않았으므로 확인할 화면이 없다

        if self._persisted(action):
            self.confirmed += 1
            return ExecutionResult(
                success=True,
                status=ActionStatus.SUCCESS,
                detail=f"{result.detail} {VERIFIED}",
                dry_run=False,
            )

        # 눌렸는지 확신할 수 없다. 다시 누르면 취소될 수 있으므로 건드리지 않는다.
        self.unknown_write += 1
        logger.error(
            "%s — 좋아요 상태를 확인하지 못했습니다(action_id=%s). 다시 누르지 않습니다.",
            UNKNOWN_WRITE_STATE,
            action.action_id,
        )
        return ExecutionResult(
            success=False,
            status=ActionStatus.FAILED,
            error=UNKNOWN_WRITE_STATE,
            detail=result.detail,
            dry_run=False,
        )

    def _persisted(self, action: QueuedAction) -> bool:
        """새로고침한 뒤에도 좋아요가 남아 있는지 확인한다."""
        page = getattr(self.inner, "_page", None)
        if page is None:
            return False
        url = self.inner._media_url(action)
        if not url:
            return False
        try:
            page.open_media(url)
            return bool(page.already_liked())
        except Exception as exc:  # noqa: BLE001 - 확인 실패는 '확인 못 함'이지 성공이 아니다
            logger.warning("좋아요 확인 중 오류(%s) — 확인되지 않은 것으로 처리합니다.", type(exc).__name__)
            return False


def build_like_only_executor(
    config, conn, *, run_id: str, page: Optional[object] = None
) -> LikeOnlyExecutor:
    """Canary용 Executor를 만든다.

    안쪽은 기존 BrowserExecutor 그대로다 — mode와 dry_run 두 스위치가 모두
    풀려야 실제로 동작한다는 기존 계약을 그대로 통과한다.
    """
    inner = BrowserExecutor(
        dry_run=config.dry_run,
        config=config,
        conn=conn,
        page=page,
    )
    return LikeOnlyExecutor(inner)
