"""Feedback 기록/집계.

v0.1은 ML 모델을 만들지 않는다. 향후 Target Score/Profile 조정에 쓸 수 있도록
Feedback 데이터를 축적하고 집계하는 구조까지만 제공한다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..core.database import utc_now
from ..core.models import FeedbackType


def record_feedback(
    conn: sqlite3.Connection,
    feedback_type: FeedbackType,
    *,
    media_pk: Optional[int] = None,
    creator_id: Optional[int] = None,
    action_id: Optional[int] = None,
    value: float = 1.0,
    note: str = "",
) -> int:
    """Feedback 한 건을 저장하고 feedback_id를 반환한다."""
    cursor = conn.execute(
        "INSERT INTO feedback (media_pk, creator_id, action_id, feedback_type, value, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (media_pk, creator_id, action_id, feedback_type.value, float(value), note, utc_now()),
    )
    return int(cursor.lastrowid)


def record_action_feedback(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    media_pk: int,
    creator_id: int,
    action_id: Optional[int] = None,
) -> Optional[int]:
    """실행 성공한 Action을 Feedback(LIKED/COMMENTED)으로 남긴다."""
    mapping = {"LIKE": FeedbackType.LIKED, "COMMENT": FeedbackType.COMMENTED}
    feedback_type = mapping.get(action_type.upper())
    if feedback_type is None:
        return None
    return record_feedback(
        conn, feedback_type, media_pk=media_pk, creator_id=creator_id, action_id=action_id
    )


@dataclass(frozen=True)
class FeedbackTarget:
    """Feedback을 붙일 대상(Action / Media / Creator)."""

    media_pk: Optional[int] = None
    creator_id: Optional[int] = None
    action_id: Optional[int] = None
    label: str = ""


def resolve_target(conn: sqlite3.Connection, raw: str) -> FeedbackTarget:
    """'12'(action_id) / '@username'(creator) / 'ABC123'(media_id)를 대상으로 변환한다."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("Feedback 대상을 지정하세요 (Action ID / @username / media_id).")

    if value.startswith("@"):
        row = conn.execute(
            "SELECT creator_id FROM creators WHERE username = ?", (value[1:],)
        ).fetchone()
        if row is None:
            raise ValueError(f"Creator를 찾을 수 없습니다: {value}")
        return FeedbackTarget(creator_id=int(row["creator_id"]), label=value)

    if value.isdigit():
        row = conn.execute(
            "SELECT action_id, media_pk, creator_id FROM action_queue WHERE action_id = ?",
            (int(value),),
        ).fetchone()
        if row is None:
            raise ValueError(f"Action을 찾을 수 없습니다: {value}")
        return FeedbackTarget(
            media_pk=int(row["media_pk"]),
            creator_id=int(row["creator_id"]),
            action_id=int(row["action_id"]),
            label=f"action:{value}",
        )

    row = conn.execute(
        "SELECT media_pk, creator_id FROM candidate_media WHERE media_id = ?", (value,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Media를 찾을 수 없습니다: {value}")
    return FeedbackTarget(
        media_pk=int(row["media_pk"]), creator_id=int(row["creator_id"]), label=f"media:{value}"
    )


def record_for_target(
    conn: sqlite3.Connection,
    feedback_type: FeedbackType,
    target: FeedbackTarget,
    note: str = "",
) -> int:
    """해석된 대상에 Feedback을 기록한다."""
    return record_feedback(
        conn,
        feedback_type,
        media_pk=target.media_pk,
        creator_id=target.creator_id,
        action_id=target.action_id,
        note=note,
    )


def feedback_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Feedback 타입별 누적 건수."""
    rows = conn.execute(
        "SELECT feedback_type, COUNT(*) AS n FROM feedback GROUP BY feedback_type"
    ).fetchall()
    return {row["feedback_type"]: int(row["n"]) for row in rows}
