"""Dashboard 상태 변경 서비스 (Phase 14).

UI(app.py)와 분리해 단위 테스트가 가능하게 한다.
여기서 하는 일은 **DB 상태 변경뿐**이다.
- 실제 Instagram Action을 실행하지 않는다.
- Claude Runtime을 호출하지 않는다.
- 같은 버튼을 여러 번 눌러도 중복 생성되지 않는다(UNIQUE 제약 + 멱등 처리).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

from ..analysis.similarity import normalize_text
from ..core.config import Config
from ..core.database import enqueue_action, transaction, update_candidate_status, utc_now
from ..core.logger import get_logger
from ..core.models import ActionStatus, ActionType, DraftStatus, FeedbackType, MediaStatus
from ..learning.feedback import record_feedback

logger = get_logger("dashboard.service")

OPERATOR_GENERATOR = "operator"


@dataclass
class ActionOutcome:
    """승인 결과."""

    created: list[str]
    already: list[str]
    warnings: list[str]

    @property
    def message(self) -> str:
        parts = []
        if self.created:
            parts.append(f"승인: {', '.join(self.created)}")
        if self.already:
            parts.append(f"이미 승인됨: {', '.join(self.already)}")
        if self.warnings:
            parts.append(" / ".join(self.warnings))
        return " · ".join(parts) or "변경 없음"


class DashboardService:
    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.conn = conn
        self.config = config

    # --- 댓글 -----------------------------------------------------------
    def select_comment(
        self, media_pk: int, *, draft_id: Optional[int] = None, text: Optional[str] = None
    ) -> Optional[int]:
        """댓글 후보를 선택하거나, 운영자가 수정한 문장을 선택으로 저장한다.

        원본 Draft는 지우지 않는다. 수정본은 generator='operator' 행으로 따로 남긴다.
        """
        edited = normalize_text(text or "")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE comment_drafts SET status = ? WHERE media_pk = ? AND status = ?",
                (DraftStatus.CANDIDATE.value, media_pk, DraftStatus.SELECTED.value),
            )

            target_id = draft_id
            if edited:
                existing = self.conn.execute(
                    "SELECT draft_id FROM comment_drafts WHERE media_pk = ? AND normalized_text = ?",
                    (media_pk, edited.lower()),
                ).fetchone()
                if existing:
                    target_id = int(existing["draft_id"])
                else:
                    cursor = self.conn.execute(
                        "INSERT INTO comment_drafts (media_pk, text, normalized_text, language, "
                        "length, quality_ok, quality_reason, similarity_max, status, generator, "
                        "generator_version, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 1, 'operator_edit', 0, ?, ?, '1', ?)",
                        (
                            media_pk,
                            edited,
                            edited.lower(),
                            str(self.config.get("comments.language", "ko")),
                            len(edited),
                            DraftStatus.SELECTED.value,
                            OPERATOR_GENERATOR,
                            utc_now(),
                        ),
                    )
                    target_id = int(cursor.lastrowid)

            if target_id is None:
                return None
            self.conn.execute(
                "UPDATE comment_drafts SET status = ? WHERE draft_id = ? AND media_pk = ?",
                (DraftStatus.SELECTED.value, int(target_id), media_pk),
            )
            # 이미 만들어진 COMMENT Action이 있으면 선택한 문장으로 맞춘다.
            row = self.conn.execute(
                "SELECT text FROM comment_drafts WHERE draft_id = ?", (int(target_id),)
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE action_queue SET comment_text = ?, draft_id = ?, updated_at = ? "
                    "WHERE media_pk = ? AND action_type = 'COMMENT' "
                    "AND status IN ('PENDING', 'APPROVED')",
                    (row["text"], int(target_id), utc_now(), media_pk),
                )
        return int(target_id)

    def selected_comment(self, media_pk: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT draft_id, text FROM comment_drafts WHERE media_pk = ? AND status = ? "
            "ORDER BY draft_id DESC LIMIT 1",
            (media_pk, DraftStatus.SELECTED.value),
        ).fetchone()

    # --- Action 승인 -----------------------------------------------------
    def approve(
        self, media_pk: int, action_types: Sequence[ActionType], *, run_id: str = "dashboard"
    ) -> ActionOutcome:
        """운영자가 고른 Action을 Queue에 올린다(실행하지 않는다).

        status는 PENDING으로 둔다. Phase 9의 APPROVED는 '수동 처리 후 확인 대기'를
        뜻하므로, 승인 사실은 approved_at 컬럼으로 따로 기록한다.
        """
        media = self.conn.execute(
            "SELECT media_pk, creator_id, target_score FROM candidate_media WHERE media_pk = ?",
            (media_pk,),
        ).fetchone()
        if media is None:
            return ActionOutcome([], [], ["후보를 찾을 수 없습니다."])

        outcome = ActionOutcome([], [], list(self._limit_warnings(action_types)))
        selected = self.selected_comment(media_pk)
        score = float(media["target_score"] or 0)
        now = utc_now()

        with transaction(self.conn):
            for action_type in action_types:
                comment_text = selected["text"] if action_type is ActionType.COMMENT and selected else None
                if action_type is ActionType.COMMENT and not comment_text:
                    outcome.warnings.append("선택된 댓글이 없어 COMMENT는 승인하지 않았습니다.")
                    continue

                action_id = enqueue_action(
                    self.conn,
                    run_id=run_id,
                    media_pk=media_pk,
                    creator_id=int(media["creator_id"]),
                    action_type=action_type,
                    target_score=score,
                    priority=int(round(score)) + (1 if action_type is ActionType.COMMENT else 0),
                    executor=self.config.executor_mode,
                    dry_run=self.config.dry_run,
                    draft_id=selected["draft_id"] if comment_text else None,
                    comment_text=comment_text,
                )
                if action_id is None:
                    # UNIQUE(media_pk, action_type) — 중복 생성 대신 승인 시각만 채운다.
                    existing = self.conn.execute(
                        "SELECT action_id, approved_at, status FROM action_queue "
                        "WHERE media_pk = ? AND action_type = ?",
                        (media_pk, action_type.value),
                    ).fetchone()
                    if existing and not existing["approved_at"]:
                        self.conn.execute(
                            "UPDATE action_queue SET approved_at = ?, updated_at = ? WHERE action_id = ?",
                            (now, now, int(existing["action_id"])),
                        )
                        outcome.created.append(action_type.value)
                    else:
                        outcome.already.append(action_type.value)
                    continue

                self.conn.execute(
                    "UPDATE action_queue SET approved_at = ?, updated_at = ? WHERE action_id = ?",
                    (now, now, action_id),
                )
                outcome.created.append(action_type.value)

            if outcome.created or outcome.already:
                update_candidate_status(self.conn, media_pk, MediaStatus.QUEUED, "dashboard_approved")
        return outcome

    def _limit_warnings(self, action_types: Sequence[ActionType]) -> list[str]:
        """일일 한도 초과 여부를 알려준다(규칙은 기존 Rate Limiter 기준 그대로)."""
        from ..actions.rate_limiter import RateLimiter

        limiter = RateLimiter(self.config, self.conn)
        warnings: list[str] = []
        for action_type in action_types:
            used, limit = limiter.usage_summary().get(action_type.value, (0, 0))
            # limit=0은 '오늘은 하지 않음' 설정이므로 이 경우에도 경고해야 한다.
            if used >= limit:
                warnings.append(
                    f"{action_type.value} 일일 한도 초과({used}/{limit}) — 실행 시 건너뜁니다."
                )
        return warnings

    # --- Skip / Undo -----------------------------------------------------
    def skip(self, media_pk: int, reason: str = "dashboard_skip") -> bool:
        """후보를 보류한다. 분석 결과와 댓글 Draft는 남긴다."""
        with transaction(self.conn):
            update_candidate_status(self.conn, media_pk, MediaStatus.SKIPPED, reason)
            self.conn.execute(
                "UPDATE action_queue SET status = ?, result = ?, updated_at = ? "
                "WHERE media_pk = ? AND status = 'PENDING'",
                (ActionStatus.CANCELLED.value, reason, utc_now(), media_pk),
            )
        return True

    def reopen(self, media_pk: int) -> tuple[bool, str]:
        """검토 대기로 되돌린다. 이미 실행된 Action이 있으면 거부한다."""
        executed = self.conn.execute(
            "SELECT 1 FROM interactions WHERE media_pk = ? LIMIT 1", (media_pk,)
        ).fetchone()
        if executed:
            return False, "이미 실행된 Interaction이 있어 되돌릴 수 없습니다."

        with transaction(self.conn):
            update_candidate_status(self.conn, media_pk, MediaStatus.SCORED, "dashboard_reopen")
            self.conn.execute(
                "UPDATE action_queue SET status = ?, approved_at = NULL, result = 'reopened', "
                "updated_at = ? WHERE media_pk = ? AND status IN ('PENDING', 'APPROVED')",
                (ActionStatus.CANCELLED.value, utc_now(), media_pk),
            )
        return True, "검토 대기로 되돌렸습니다."

    # --- Feedback ---------------------------------------------------------
    def add_feedback(
        self, media_pk: int, feedback_type: FeedbackType, note: str = ""
    ) -> tuple[bool, str]:
        """같은 후보에 같은 Feedback을 중복 저장하지 않는다."""
        media = self.conn.execute(
            "SELECT creator_id FROM candidate_media WHERE media_pk = ?", (media_pk,)
        ).fetchone()
        if media is None:
            return False, "후보를 찾을 수 없습니다."

        existing = self.conn.execute(
            "SELECT 1 FROM feedback WHERE media_pk = ? AND feedback_type = ? LIMIT 1",
            (media_pk, feedback_type.value),
        ).fetchone()
        if existing:
            return False, f"{feedback_type.value}는 이미 기록되어 있습니다."

        with transaction(self.conn):
            record_feedback(
                self.conn,
                feedback_type,
                media_pk=media_pk,
                creator_id=int(media["creator_id"]),
                note=note,
            )
        return True, f"{feedback_type.value} 기록됨"
