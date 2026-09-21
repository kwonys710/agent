"""Feedback 기록/집계.

v0.1은 ML 모델을 만들지 않는다. 향후 Target Score/Profile 조정에 쓸 수 있도록
Feedback 데이터를 축적하고 집계하는 구조까지만 제공한다.
"""
from __future__ import annotations

import sqlite3
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


def feedback_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Feedback 타입별 누적 건수."""
    rows = conn.execute(
        "SELECT feedback_type, COUNT(*) AS n FROM feedback GROUP BY feedback_type"
    ).fetchall()
    return {row["feedback_type"]: int(row["n"]) for row in rows}
