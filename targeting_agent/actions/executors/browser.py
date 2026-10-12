"""BrowserActionExecutor — 운영자 로그인 세션으로 LIKE/COMMENT를 수행한다 (Phase 18B).

Phase 9에서 보류한 BrowserExecutor를, Phase 18A에서 만든 "운영자가 직접 로그인한
persistent profile" 구조 위에서 구현한다. 기존 Action Queue / Rate Limiter /
Approval Gate / Interaction 기록을 **그대로 재사용**한다(두 번째 Queue를 만들지 않는다).

수행하는 Action: LIKE, COMMENT 두 가지뿐.
FOLLOW / DM / SAVE / SHARE는 구현하지 않는다.

Mode:
    DISABLED  아무 것도 하지 않는다(Action을 SKIPPED로 남긴다)
    DRY_RUN   브라우저를 열지 않는다. 무엇을 실행할지와 한도 통과 여부까지만 확인한다(기본값)
    LIVE      실제로 좋아요/댓글을 수행한다

안전 규칙:
- `actions.execution.dry_run`이 true면 mode가 LIVE여도 실제 동작을 하지 않는다(강한 가드).
- 로그인 필요 / Challenge / 경고 / 작업 차단 감지 → PlatformWarningError로 Run 전체 중단.
- 실패한 Action을 재시도하지 않는다. 탐지 우회를 하지 않는다.
- 같은 게시물에 이미 완료한 Action은 다시 실행하지 않는다.
"""
from __future__ import annotations

import sqlite3
from enum import Enum
from typing import Optional

from ...core.config import Config
from ...core.database import has_interaction
from ...core.exceptions import PlatformWarningError
from ...core.logger import get_logger
from ...core.models import ActionType, ExecutionResult, QueuedAction
from .base import BaseExecutor
from .browser_page import ActionPage

logger = get_logger("actions.executor.browser")


class ExecutorMode(str, Enum):
    DISABLED = "DISABLED"
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"

    @classmethod
    def parse(cls, raw: Optional[str]) -> "ExecutorMode":
        """알 수 없는 값은 가장 안전한 DRY_RUN으로 떨어뜨린다."""
        text = str(raw or "").strip().upper()
        for mode in cls:
            if mode.value == text:
                return mode
        if text:
            logger.warning("알 수 없는 browser_executor.mode=%s → DRY_RUN으로 처리합니다.", raw)
        return cls.DRY_RUN


class BrowserExecutor(BaseExecutor):
    """브라우저로 LIKE/COMMENT를 수행하는 Executor."""

    name = "browser"
    supports_like = True
    supports_comment = True

    def __init__(
        self,
        dry_run: bool = True,
        *,
        config: Optional[Config] = None,
        conn: Optional[sqlite3.Connection] = None,
        page: Optional[ActionPage] = None,
        mode: Optional[str] = None,
    ) -> None:
        super().__init__(dry_run=dry_run)
        self.config = config
        self.conn = conn
        self._page = page
        self._owns_page = page is None
        raw_mode = mode if mode is not None else (
            config.get("browser_executor.mode", ExecutorMode.DRY_RUN.value) if config else None
        )
        self.mode = ExecutorMode.parse(raw_mode)
        # config의 dry_run이 켜져 있으면 LIVE여도 실제 동작을 하지 않는다.
        self.live = self.mode is ExecutorMode.LIVE and not dry_run
        if self.mode is ExecutorMode.LIVE and dry_run:
            logger.warning(
                "browser_executor.mode=LIVE 이지만 actions.execution.dry_run=true 이므로 "
                "실제 동작은 하지 않습니다."
            )

    # --- 세션 -------------------------------------------------------------
    def validate_session(self) -> bool:
        if self.mode is ExecutorMode.DISABLED:
            logger.info("browser_executor.mode=DISABLED — Action을 실행하지 않습니다.")
            return True  # Run 자체는 진행하고, 각 Action을 SKIPPED로 남긴다
        if not self.live:
            return True  # DRY_RUN은 브라우저를 열지 않는다
        page = self._ensure_page()
        state = page.ensure_logged_in()
        if not state.can_continue:
            logger.error("브라우저 세션이 Action을 수행할 수 없는 상태입니다: %s", state.value)
            return False
        reason = page.stop_reason()
        if reason:
            logger.error("Action을 시작할 수 없는 상태입니다: %s", reason)
            return False
        return True

    def health_check(self) -> dict[str, object]:
        info = super().health_check()
        info["mode"] = self.mode.value
        info["live"] = self.live
        return info

    def finalize(self) -> None:
        if self._owns_page and self._page is not None:
            try:
                self._page.close()
            except Exception:  # noqa: BLE001 - 종료 실패는 결과를 바꾸지 않는다
                logger.warning("브라우저 종료 실패(무시)")
            self._page = None

    # --- Action -----------------------------------------------------------
    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        return self._execute(action, ActionType.LIKE)

    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        if not (action.comment_text or "").strip():
            return self._skipped("no_comment_text")
        return self._execute(action, ActionType.COMMENT)

    def _execute(self, action: QueuedAction, action_type: ActionType) -> ExecutionResult:
        if self.mode is ExecutorMode.DISABLED:
            return self._skipped("browser_executor_disabled")

        if self._already_done(action, action_type):
            return self._skipped("already_executed")

        target = self._media_url(action)
        if not target:
            return self._skipped("no_media_url")

        plan = f"{action_type.value} {target}"
        if not self.live:
            # DRY_RUN: 브라우저를 열지 않는다. 무엇을 할지와 댓글 내용까지만 기록한다.
            detail = f"dry_run:{plan}"
            if action_type is ActionType.COMMENT:
                detail += f" comment={(action.comment_text or '').strip()[:60]}"
            logger.info("[DRY_RUN] %s", detail)
            return self._success(detail)

        page = self._ensure_page()
        page.open_media(target)
        self._halt_if_blocked(page, plan)

        if action_type is ActionType.LIKE:
            if page.already_liked():
                return self._skipped("already_liked")
            ok = page.like()
        else:
            ok = page.comment(str(action.comment_text or ""))

        # 동작 직후에도 차단/경고가 뜰 수 있다 — 성공 판정보다 먼저 확인한다.
        self._halt_if_blocked(page, plan)

        if not ok:
            # 재시도하지 않는다. 실패 사유는 Action에 기록된다.
            return self._failed(f"{action_type.value.lower()}_not_confirmed")
        return self._success(plan)

    # --- 내부 -------------------------------------------------------------
    def _already_done(self, action: QueuedAction, action_type: ActionType) -> bool:
        """같은 게시물에 이미 **실제로** 수행한 Action인지 확인한다(DB 기록 기준).

        Dry Run 기록은 실제 동작이 아니므로 중복으로 보지 않는다 — 그렇지 않으면
        DRY_RUN을 한 번 돌린 뒤에는 LIVE에서 아무 것도 실행되지 않는다.
        """
        if self.conn is None:
            return False
        try:
            return bool(
                has_interaction(
                    self.conn, action.media_pk, dry_run=False, action_type=action_type
                )
            )
        except sqlite3.Error:  # pragma: no cover - 조회 실패 시 막지 않는다
            logger.warning("Interaction 조회 실패 — 중복 검사를 건너뜁니다.")
            return False

    def _media_url(self, action: QueuedAction) -> str:
        """canonical_url을 우선 사용한다(쿼리 제거된 정규 URL)."""
        if self.conn is not None:
            row = self.conn.execute(
                "SELECT canonical_url, permalink FROM candidate_media WHERE media_pk = ?",
                (action.media_pk,),
            ).fetchone()
            if row is not None:
                return str(row["canonical_url"] or row["permalink"] or "")
        return str(getattr(action, "permalink", "") or "")

    def _halt_if_blocked(self, page: ActionPage, plan: str) -> None:
        reason = page.stop_reason()
        if reason:
            logger.error("중단 상태 감지(%s) — Action Run 전체를 중단합니다: %s", reason, plan)
            raise PlatformWarningError(f"{reason}:{plan}")

    def _ensure_page(self) -> ActionPage:
        if self._page is not None:
            return self._page
        if self.config is None:
            raise PlatformWarningError("browser executor에 config가 없어 브라우저를 열 수 없습니다.")
        from .browser_page import PlaywrightActionPage

        page = PlaywrightActionPage(
            profile_dir=self.config._resolve_path(
                self.config.get("browser_discovery.profile_dir", "data/browser_profile")
            ),
            headless=bool(self.config.get("browser_executor.headless", False)),
            timeout_ms=int(self.config.get("browser_executor.timeout_ms", 20000)),
            selector_timeout_ms=int(self.config.get("browser_executor.selector_timeout_ms", 4000)),
            executable_path=str(self.config.get("browser_discovery.executable_path", "") or "")
            or None,
        )
        page.start()
        self._page = page
        return page
