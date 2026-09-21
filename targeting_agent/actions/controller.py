"""Action Controller.

Action Queue를 읽어 Rate Limit을 확인하고 Executor로 실행한 뒤
Interaction/통계/상태를 기록한다. Executor는 config로 교체 가능하다.

중단 조건:
- 연속/누적 오류가 execution.stop_if_error_count 이상
- 플랫폼 Warning/Challenge/로그인 요구 감지(PlatformWarningError)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..core.config import Config
from ..core.database import (
    bump_stat,
    fetch_pending_actions,
    record_interaction,
    today_str,
    update_action_status,
    update_candidate_status,
)
from ..core.exceptions import PlatformWarningError
from ..core.logger import get_logger
from ..core.models import ActionStatus, ActionType, MediaStatus, QueuedAction
from ..learning.feedback import record_action_feedback
from .executors.base import BaseExecutor
from .rate_limiter import RateLimiter

logger = get_logger("actions.controller")


@dataclass
class ExecutionSummary:
    success: int = 0
    skipped: int = 0
    failed: int = 0
    awaiting: int = 0  # 목록에 올렸고 운영자 확인을 기다리는 건수(실제 모드)
    halted: bool = False
    halt_reason: str = ""
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


def row_to_action(row: Mapping[str, Any]) -> QueuedAction:
    return QueuedAction(
        action_id=int(row["action_id"]),
        media_pk=int(row["media_pk"]),
        media_id=str(row["media_id"]),
        permalink=str(row["permalink"]),
        username=str(row["username"]),
        creator_id=int(row["creator_id"]),
        action_type=ActionType(str(row["action_type"])),
        target_score=float(row["target_score"] or 0),
        priority=int(row["priority"] or 0),
        status=ActionStatus(str(row["status"])),
        comment_text=row["comment_text"],
        draft_id=row["draft_id"],
        executor=str(row["executor"]),
    )


class ActionController:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        executor: BaseExecutor,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.executor = executor
        self.dry_run = config.dry_run
        self.rate_limiter = rate_limiter or RateLimiter(config, conn, dry_run=self.dry_run)
        self.stop_if_error_count = int(config.get("actions.execution.stop_if_error_count", 3))
        self.max_actions_per_run = int(config.get("actions.execution.max_actions_per_run", 10))
        self.halt_on_platform_warning = bool(config.get("safety.halt_on_platform_warning", True))
        self.learning_enabled = bool(config.get("learning.enabled", True))
        self._date = today_str(int(config.get("actions.daily_limits.timezone_offset_hours", 9)))

    def run(self) -> ExecutionSummary:
        """PENDING/APPROVED Action을 순서대로 실행한다."""
        summary = ExecutionSummary()

        if not self.executor.validate_session():
            summary.halted = True
            summary.halt_reason = f"executor_session_invalid:{self.executor.name}"
            logger.error("Executor 세션이 유효하지 않아 실행을 중단합니다: %s", self.executor.name)
            return summary

        actions = fetch_pending_actions(self.conn, limit=self.max_actions_per_run or None)
        logger.info("실행 대상 Action %d건 (dry_run=%s)", len(actions), self.dry_run)

        errors = 0
        try:
            for row in actions:
                action = row_to_action(row)
                decision = self.rate_limiter.check(
                    action.action_type, action.creator_id, action.media_pk
                )
                if not decision.allowed:
                    summary.skipped += 1
                    summary.note_skip(decision.reason)
                    update_action_status(
                        self.conn,
                        action.action_id,
                        ActionStatus.SKIPPED,
                        result=decision.reason,
                        finished=True,
                    )
                    logger.info(
                        "SKIP %s @%s — %s",
                        action.action_type.value,
                        action.username,
                        decision.reason,
                    )
                    continue

                result = self._execute_one(action)

                if result.status is ActionStatus.SUCCESS:
                    summary.success += 1
                    self.rate_limiter.record_executed()
                    bump_stat(self.conn, self._date, f"{action.action_type.value.lower()}_success")
                elif result.status is ActionStatus.APPROVED:
                    # 실제 처리는 운영자가 한다. 확인(--confirm) 전까지 Interaction으로 세지 않는다.
                    summary.awaiting += 1
                    self.rate_limiter.record_executed()
                elif result.status is ActionStatus.SKIPPED:
                    summary.skipped += 1
                    summary.note_skip(result.detail or "executor_skip")
                else:
                    summary.failed += 1
                    errors += 1
                    bump_stat(self.conn, self._date, "errors")

                self.conn.commit()

                if self.stop_if_error_count and errors >= self.stop_if_error_count:
                    summary.halted = True
                    summary.halt_reason = f"error_count>={self.stop_if_error_count}"
                    logger.error("오류 %d건으로 실행을 중단합니다.", errors)
                    break
        except PlatformWarningError as exc:
            summary.halted = True
            summary.halt_reason = f"platform_warning:{exc}"
            logger.error("플랫폼 경고 감지 — 자동 실행을 즉시 중단합니다: %s", exc)
            self.conn.commit()
        finally:
            self.executor.finalize()

        return summary

    def _execute_one(self, action: QueuedAction):
        update_action_status(self.conn, action.action_id, ActionStatus.RUNNING, started=True)
        try:
            if action.action_type is ActionType.LIKE:
                result = self.executor.execute_like(action)
            else:
                result = self.executor.execute_comment(action)
        except PlatformWarningError:
            update_action_status(
                self.conn,
                action.action_id,
                ActionStatus.CANCELLED,
                result="platform_warning",
                finished=True,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - Executor 오류를 Action 단위로 격리
            logger.exception("Action 실행 실패: action_id=%s", action.action_id)
            update_action_status(
                self.conn,
                action.action_id,
                ActionStatus.FAILED,
                result="",
                error=str(exc),
                finished=True,
            )
            record_interaction(
                self.conn,
                media_pk=action.media_pk,
                creator_id=action.creator_id,
                action_id=action.action_id,
                action_type=action.action_type,
                executor=self.executor.name,
                dry_run=self.dry_run,
                success=False,
                comment_text=action.comment_text,
                error=str(exc),
            )
            from ..core.models import ExecutionResult

            return ExecutionResult(
                success=False, status=ActionStatus.FAILED, error=str(exc), dry_run=self.dry_run
            )

        update_action_status(
            self.conn,
            action.action_id,
            result.status,
            result=result.detail,
            error=result.error,
            finished=True,
        )
        if result.status is ActionStatus.SUCCESS:
            record_interaction(
                self.conn,
                media_pk=action.media_pk,
                creator_id=action.creator_id,
                action_id=action.action_id,
                action_type=action.action_type,
                executor=self.executor.name,
                dry_run=self.dry_run,
                success=True,
                comment_text=action.comment_text,
            )
            update_candidate_status(
                self.conn, action.media_pk, MediaStatus.INTERACTED, action.action_type.value
            )
            if self.learning_enabled:
                record_action_feedback(
                    self.conn,
                    action_type=action.action_type.value,
                    media_pk=action.media_pk,
                    creator_id=action.creator_id,
                    action_id=action.action_id,
                )
        return result
