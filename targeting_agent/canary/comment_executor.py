"""COMMENT 전용 Canary Executor (Phase 19M).

LIKE Canary(`canary/executor.py`)의 거울이다. 그쪽이 COMMENT를 코드 수준에서 막듯,
이쪽은 **COMMENT만** 수행하고 LIKE는 건너뛴다. 하나의 Run이 한 가지 일만 하게 해서
무엇이 일어났는지 사후에 분명히 읽히도록 한다.

COMMENT는 LIKE와 위험이 다르다 — 공개 텍스트가 남고 되돌리기가 번거롭다. 그래서:
- 게시 결과를 **확인**한다(목록 확인 + 새로고침 후에도 남아 있는지 — Phase 19G).
- 확인 불가(UNKNOWN)면 **Run 전체를 멈춘다.** 같은 댓글을 다시 보내지 않는다.
- 한 Creator에 한 건, 실행당 상한(기본 1건)을 코드에서 막는다.
- 댓글 문구는 기존 품질 Gate를 통과한 후보만 쓴다(여기서 만들지 않는다).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from ..actions.executors.base import BaseExecutor
from ..actions.executors.browser import BrowserExecutor
from ..actions.executors.browser_page import (
    COMMENT_CONFIRMED,
    COMMENT_NOT_SENT,
    COMMENT_UNKNOWN_STATE,
)
from ..actions.policy import comment_eligibility
from ..core.config import Config
from ..core.exceptions import PlatformWarningError
from ..core.logger import get_logger
from ..core.models import ActionType, ExecutionResult, QueuedAction

logger = get_logger("canary.comment_executor")

HALT_UNKNOWN = "UNKNOWN_COMMENT_STATE"
SKIP_LIKE = "comment_canary_like_skipped"
SKIP_CAP = "comment_canary_cap_reached"
SKIP_CREATOR = "comment_canary_creator_done"
SKIP_NOT_ELIGIBLE = "comment_canary_not_eligible"


class CommentOnlyExecutor(BaseExecutor):
    """COMMENT만 수행하는 Canary Executor. 상한과 확인을 코드에서 보장한다."""

    name = "canary_comment"
    supports_like = False
    supports_comment = True

    def __init__(
        self,
        inner: BrowserExecutor,
        config: Config,
        conn: sqlite3.Connection,
        *,
        max_comments: int = 1,
    ) -> None:
        super().__init__(dry_run=inner.dry_run)
        self.inner = inner
        self.config = config
        self.conn = conn
        self.max_comments = max(0, int(max_comments))
        self.confirmed = 0
        self.unknown = 0
        self.not_sent = 0
        self.like_skipped = 0
        self.creators: set[int] = set()
        self.posted: list[str] = []
        self.halt_reason: Optional[str] = None

    # --- LIKE는 이 Run의 일이 아니다 --------------------------------------
    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        self.like_skipped += 1
        logger.info("COMMENT Canary — LIKE는 이 Run에서 실행하지 않습니다(action %s).", action.action_id)
        return self._skipped(SKIP_LIKE)

    # --- COMMENT ----------------------------------------------------------
    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        if self.confirmed >= self.max_comments:
            return self._skipped(SKIP_CAP)
        if action.creator_id in self.creators:
            # 같은 Creator에게 한 Run에서 두 번 달지 않는다.
            return self._skipped(SKIP_CREATOR)

        # 실행 직전에 품질 Gate를 다시 본다 — 큐에 들어간 뒤 상황이 바뀔 수 있다.
        eligibility = comment_eligibility(
            self.conn, self.config, action.media_pk, action.target_score
        )
        if not eligibility.ok:
            logger.info("COMMENT Canary — 자격 미달로 건너뜁니다(%s).", eligibility.reason)
            return self._skipped(f"{SKIP_NOT_ELIGIBLE}:{eligibility.reason}")

        text = (action.comment_text or eligibility.text or "").strip()
        if not text:
            return self._skipped("no_comment_text")

        state, detail = self._post(action, text)

        if state == COMMENT_CONFIRMED:
            self.confirmed += 1
            self.creators.add(action.creator_id)
            self.posted.append(text)
            logger.info("COMMENT Canary — 게시 확인(action %s).", action.action_id)
            return self._success(f"comment_confirmed:{text[:40]}")

        if state == COMMENT_UNKNOWN_STATE:
            # 보냈는지 확인할 수 없다. 재시도하면 공개 중복 댓글 위험이 있다.
            self.unknown += 1
            self.creators.add(action.creator_id)
            self.halt_reason = f"{HALT_UNKNOWN}:{detail or 'unverified'}"
            logger.error(
                "COMMENT Canary — 게시 여부를 확인할 수 없어 Run을 중단합니다(%s). "
                "같은 댓글을 다시 보내지 않습니다.",
                detail or "-",
            )
            raise PlatformWarningError(self.halt_reason)

        self.not_sent += 1
        return self._failed(f"{COMMENT_NOT_SENT}:{detail or 'unknown'}")

    def _post(self, action: QueuedAction, text: str) -> tuple[str, str]:
        """실제 게시. DRY_RUN이면 브라우저를 열지 않고 계획만 확인한다."""
        if not self.inner.live:
            logger.info("[DRY_RUN] COMMENT %s ← %s", action.media_id, text[:40])
            return COMMENT_CONFIRMED, "dry_run"

        page: Any = self.inner._ensure_page()
        self.inner._halt_if_blocked(page, f"COMMENT {action.media_id}")
        url = self.inner._media_url(action)
        if not url:
            return COMMENT_NOT_SENT, "no_media_url"
        page.open_media(url)
        self.inner._halt_if_blocked(page, f"COMMENT {url}")

        post_comment = getattr(page, "post_comment", None)
        if post_comment is None:  # pragma: no cover - 구형 page 구현 대비
            return (COMMENT_CONFIRMED, "legacy_ok") if page.comment(text) else (
                COMMENT_NOT_SENT,
                "legacy_fail",
            )
        outcome = post_comment(text)
        # 동작 직후 차단/경고가 뜰 수 있다 — 성공 판정보다 먼저 본다.
        self.inner._halt_if_blocked(page, f"COMMENT {url}")
        return outcome.state, getattr(outcome, "detail", "")

    def health_check(self) -> dict[str, object]:
        info = super().health_check()
        info.update(
            {
                "mode": "COMMENT_CANARY",
                "live": self.inner.live,
                "max_comments": self.max_comments,
                "confirmed": self.confirmed,
                "unknown": self.unknown,
            }
        )
        return info

    def validate_session(self) -> bool:
        return self.inner.validate_session()

    def finalize(self) -> None:
        self.inner.finalize()


def build_comment_only_executor(
    config: Config,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    page: Optional[object] = None,
    max_comments: int = 1,
) -> CommentOnlyExecutor:
    """Canary용 COMMENT Executor. 안쪽은 기존 BrowserExecutor 그대로다.

    mode=LIVE와 dry_run=false 두 스위치가 모두 풀려야 실제로 동작한다는
    기존 계약을 그대로 통과한다.
    """
    inner = BrowserExecutor(dry_run=config.dry_run, config=config, conn=conn, page=page)
    return CommentOnlyExecutor(inner, config, conn, max_comments=max_comments)
