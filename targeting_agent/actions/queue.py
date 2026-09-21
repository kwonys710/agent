"""Action Queue 생성.

Target Score와 config 임계값에 따라 LIKE / COMMENT Action을 만든다.
- score >= require_score_for_like    -> LIKE
- score >= require_score_for_comment -> LIKE + COMMENT (댓글 후보가 있을 때)
- 그 외                               -> SKIP
동일 media+action_type은 DB UNIQUE 제약으로 중복 적재되지 않는다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ..core.config import Config
from ..core.database import enqueue_action, update_candidate_status
from ..core.logger import get_logger
from ..core.models import ActionType, MediaStatus

logger = get_logger("actions.queue")


@dataclass
class QueueBuildResult:
    likes: int = 0
    comments: int = 0
    skipped: int = 0
    duplicates: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.likes + self.comments

    def _note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class ActionQueueBuilder:
    def __init__(self, config: Config, run_id: str) -> None:
        self.config = config
        self.run_id = run_id
        self.enable_like = bool(config.get("actions.enable_like", True))
        self.enable_comment = bool(config.get("actions.enable_comment", True))
        self.like_threshold = float(config.get("actions.require_score_for_like", 75))
        self.comment_threshold = float(config.get("actions.require_score_for_comment", 82))
        self.minimum_score = float(config.get("scoring.minimum_target_score", 70))
        self.executor = config.executor_mode
        self.dry_run = config.dry_run

    def build(
        self,
        conn: sqlite3.Connection,
        media_row: Mapping[str, Any],
        target_score: float,
        comment_draft_id: Optional[int] = None,
        comment_text: Optional[str] = None,
    ) -> QueueBuildResult:
        """후보 1건에 대한 Action을 큐에 넣는다."""
        result = QueueBuildResult()
        media_pk = int(media_row["media_pk"])
        creator_id = int(media_row["creator_id"])
        priority = int(round(target_score))

        if target_score < self.minimum_score:
            result.skipped += 1
            result._note("below_minimum_score")
            update_candidate_status(
                conn, media_pk, MediaStatus.SKIPPED, f"score<{self.minimum_score}", target_score
            )
            return result

        queued_any = False

        if self.enable_like and target_score >= self.like_threshold:
            action_id = enqueue_action(
                conn,
                run_id=self.run_id,
                media_pk=media_pk,
                creator_id=creator_id,
                action_type=ActionType.LIKE,
                target_score=target_score,
                priority=priority,
                executor=self.executor,
                dry_run=self.dry_run,
            )
            if action_id:
                result.likes += 1
                queued_any = True
            else:
                result.duplicates += 1
                result._note("duplicate_like")
        elif self.enable_like:
            result._note("below_like_threshold")

        if self.enable_comment and target_score >= self.comment_threshold:
            if comment_text:
                action_id = enqueue_action(
                    conn,
                    run_id=self.run_id,
                    media_pk=media_pk,
                    creator_id=creator_id,
                    action_type=ActionType.COMMENT,
                    target_score=target_score,
                    priority=priority + 1,  # 같은 점수면 COMMENT를 먼저 처리
                    executor=self.executor,
                    dry_run=self.dry_run,
                    draft_id=comment_draft_id,
                    comment_text=comment_text,
                )
                if action_id:
                    result.comments += 1
                    queued_any = True
                else:
                    result.duplicates += 1
                    result._note("duplicate_comment")
            else:
                result._note("no_comment_candidate")

        if queued_any:
            update_candidate_status(conn, media_pk, MediaStatus.QUEUED, "queued", target_score)
        else:
            result.skipped += 1
            update_candidate_status(
                conn, media_pk, MediaStatus.SKIPPED, "no_action_queued", target_score
            )
        return result


def merge_results(results: Sequence[QueueBuildResult]) -> QueueBuildResult:
    merged = QueueBuildResult()
    for item in results:
        merged.likes += item.likes
        merged.comments += item.comments
        merged.skipped += item.skipped
        merged.duplicates += item.duplicates
        for reason, count in item.reasons.items():
            merged.reasons[reason] = merged.reasons.get(reason, 0) + count
    return merged
