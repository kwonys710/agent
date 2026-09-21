"""수동 처리 확인(Confirm) 흐름 — Phase 9.

`executor.mode: manual` + 실제 모드에서 Action은 목록에 오른 뒤
`APPROVED`(운영자 확인 대기) 상태로 남는다.
운영자가 Instagram에서 직접 처리한 다음 이 모듈을 통해 확인하면
그때 Interaction으로 기록된다 — 하지 않은 일을 했다고 기록하지 않기 위함이다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from ..core.config import Config
from ..core.database import (
    bump_stat,
    fetch_awaiting_actions,
    record_interaction,
    today_str,
    transaction,
    update_action_status,
    update_candidate_status,
)
from ..core.logger import get_logger
from ..core.models import ActionStatus, ActionType, MediaStatus
from ..learning.feedback import record_action_feedback

logger = get_logger("actions.confirm")

ALL_KEYWORD = "all"


@dataclass
class ConfirmResult:
    confirmed: int = 0
    skipped: int = 0
    not_found: list[int] = field(default_factory=list)
    over_limit: list[str] = field(default_factory=list)


def parse_ids(raw: str) -> tuple[bool, list[int]]:
    """'all' 또는 '12,13' 형태의 인자를 파싱한다."""
    text = (raw or "").strip().lower()
    if text == ALL_KEYWORD:
        return True, []
    ids: list[int] = []
    for token in text.replace(" ", "").split(","):
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError:
            raise ValueError(f"Action ID는 숫자여야 합니다: {token}") from None
    return False, ids


def list_awaiting(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """확인 대기 중인 Action 목록."""
    return fetch_awaiting_actions(conn)


def format_awaiting(rows: Sequence[sqlite3.Row]) -> str:
    """CLI 출력용 텍스트."""
    if not rows:
        return "확인 대기 중인 Action이 없습니다."
    lines = [f"확인 대기 Action {len(rows)}건 (처리 후 --confirm <ID|all>)", ""]
    for row in rows:
        line = (
            f"  [{row['action_id']:>4}] {row['action_type']:<7} @{row['username']:<20} "
            f"점수 {float(row['target_score'] or 0):5.1f}  {row['permalink']}"
        )
        if row["comment_text"]:
            line += f"\n         댓글: {row['comment_text']}"
        lines.append(line)
    return "\n".join(lines)


def _select(rows: Sequence[sqlite3.Row], select_all: bool, ids: Iterable[int]) -> tuple[list[sqlite3.Row], list[int]]:
    if select_all:
        return list(rows), []
    wanted = list(ids)
    by_id = {int(row["action_id"]): row for row in rows}
    selected = [by_id[i] for i in wanted if i in by_id]
    missing = [i for i in wanted if i not in by_id]
    return selected, missing


def confirm_actions(
    conn: sqlite3.Connection,
    config: Config,
    *,
    select_all: bool = False,
    action_ids: Optional[Iterable[int]] = None,
) -> ConfirmResult:
    """운영자가 직접 처리한 Action을 Interaction으로 기록한다."""
    rows = fetch_awaiting_actions(conn)
    selected, missing = _select(rows, select_all, action_ids or [])
    result = ConfirmResult(not_found=missing)

    tz_offset = int(config.get("actions.daily_limits.timezone_offset_hours", 9))
    date = today_str(tz_offset)
    limits = {
        ActionType.LIKE: int(config.get("actions.daily_limits.likes", 0)),
        ActionType.COMMENT: int(config.get("actions.daily_limits.comments", 0)),
    }
    confirmed_counts = {ActionType.LIKE: 0, ActionType.COMMENT: 0}
    learning_enabled = bool(config.get("learning.enabled", True))

    with transaction(conn):
        for row in selected:
            action_type = ActionType(str(row["action_type"]))
            action_id = int(row["action_id"])
            media_pk = int(row["media_pk"])
            creator_id = int(row["creator_id"])

            record_interaction(
                conn,
                media_pk=media_pk,
                creator_id=creator_id,
                action_id=action_id,
                action_type=action_type,
                executor=str(row["executor"]),
                dry_run=False,
                success=True,
                comment_text=row["comment_text"],
            )
            update_action_status(
                conn, action_id, ActionStatus.SUCCESS, result="manual_confirmed", finished=True
            )
            update_candidate_status(conn, media_pk, MediaStatus.INTERACTED, action_type.value)
            bump_stat(conn, date, f"{action_type.value.lower()}_success")
            if learning_enabled:
                record_action_feedback(
                    conn,
                    action_type=action_type.value,
                    media_pk=media_pk,
                    creator_id=creator_id,
                    action_id=action_id,
                )
            confirmed_counts[action_type] += 1
            result.confirmed += 1

    # 이미 처리한 일을 기록하지 않을 수는 없으므로, 한도를 넘었으면 경고만 남긴다.
    for action_type, count in confirmed_counts.items():
        limit = limits.get(action_type, 0)
        if limit and count > limit:
            message = f"{action_type.value} 확인 {count}건이 일일 한도({limit})를 넘었습니다."
            logger.warning(message)
            result.over_limit.append(message)

    logger.info("확인 완료: %d건 (대상 %d건)", result.confirmed, len(rows))
    return result


def skip_actions(
    conn: sqlite3.Connection,
    *,
    select_all: bool = False,
    action_ids: Optional[Iterable[int]] = None,
    reason: str = "manual_skip",
) -> ConfirmResult:
    """처리하지 않기로 한 Action을 취소한다(Interaction 기록 없음)."""
    rows = fetch_awaiting_actions(conn)
    selected, missing = _select(rows, select_all, action_ids or [])
    result = ConfirmResult(not_found=missing)

    with transaction(conn):
        for row in selected:
            update_action_status(
                conn,
                int(row["action_id"]),
                ActionStatus.CANCELLED,
                result=reason,
                finished=True,
            )
            update_candidate_status(
                conn, int(row["media_pk"]), MediaStatus.SKIPPED, reason
            )
            result.skipped += 1

    logger.info("취소 완료: %d건", result.skipped)
    return result
