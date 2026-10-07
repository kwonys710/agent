"""Target Profile 버전 관리 (Phase 17).

학습 결과는 기존 Profile을 덮어쓰지 않고 **새 버전을 만들어** Active로 바꾼다.
이전 버전은 그대로 남아 있어 언제든 rollback할 수 있다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Mapping, Optional

from ..core.database import transaction, utc_now
from ..core.logger import get_logger

logger = get_logger("learning.profiles")

BASE_VERSION = "v1"
SOURCE_BASE = "base"
SOURCE_LEARNING = "learning"
SOURCE_ROLLBACK = "rollback"


@dataclass(frozen=True)
class ProfileVersion:
    """저장된 Profile 한 버전."""

    version: str
    topic_weights: dict[str, float] = field(default_factory=dict)
    source: str = SOURCE_BASE
    reason: str = ""
    previous_version: Optional[str] = None
    is_active: bool = False
    created_at: str = ""

    def weight_for(self, topic: str, default: float = 1.0) -> float:
        return float(self.topic_weights.get(topic, default))


def _row_to_profile(row: sqlite3.Row) -> ProfileVersion:
    try:
        weights = json.loads(row["topic_weights"])
    except (json.JSONDecodeError, TypeError):
        weights = {}
    return ProfileVersion(
        version=str(row["version"]),
        topic_weights={str(k): float(v) for k, v in (weights or {}).items()},
        source=str(row["source"]),
        reason=str(row["reason"] or ""),
        previous_version=row["previous_version"],
        is_active=bool(row["is_active"]),
        created_at=str(row["created_at"]),
    )


def get_active_profile(conn: sqlite3.Connection) -> ProfileVersion:
    """Active Profile. 없으면 기본 버전(v1, 가중치 없음)을 만들어 반환한다."""
    row = conn.execute(
        "SELECT * FROM target_profiles WHERE is_active = 1 ORDER BY profile_id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        return _row_to_profile(row)
    return create_version(
        conn, {}, source=SOURCE_BASE, reason="초기 Profile(가중치 조정 없음)", activate=True
    )


def list_versions(conn: sqlite3.Connection, limit: int = 20) -> list[ProfileVersion]:
    rows = conn.execute(
        "SELECT * FROM target_profiles ORDER BY profile_id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [_row_to_profile(row) for row in rows]


def _next_version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COUNT(*) AS n FROM target_profiles").fetchone()
    return f"v{int(row['n']) + 1}"


def create_version(
    conn: sqlite3.Connection,
    topic_weights: Mapping[str, float],
    *,
    source: str,
    reason: str = "",
    previous_version: Optional[str] = None,
    activate: bool = True,
) -> ProfileVersion:
    """새 Profile 버전을 만든다(기존 버전은 수정하지 않는다)."""
    version = _next_version(conn)
    now = utc_now()
    with transaction(conn):
        if activate:
            conn.execute("UPDATE target_profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "INSERT INTO target_profiles (version, topic_weights, source, reason, "
            "previous_version, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                json.dumps({k: round(float(v), 4) for k, v in topic_weights.items()}, ensure_ascii=False),
                source,
                reason,
                previous_version,
                int(activate),
                now,
            ),
        )
    logger.info("Profile %s 생성(source=%s, active=%s)", version, source, activate)
    return ProfileVersion(
        version=version,
        topic_weights={str(k): float(v) for k, v in topic_weights.items()},
        source=source,
        reason=reason,
        previous_version=previous_version,
        is_active=activate,
        created_at=now,
    )


def rollback(conn: sqlite3.Connection) -> tuple[bool, str]:
    """직전 Profile을 다시 Active로 만든다. 기록은 지우지 않는다."""
    active = conn.execute(
        "SELECT * FROM target_profiles WHERE is_active = 1 ORDER BY profile_id DESC LIMIT 1"
    ).fetchone()
    if active is None:
        return False, "활성 Profile이 없습니다."

    target_version = active["previous_version"]
    if target_version:
        target = conn.execute(
            "SELECT * FROM target_profiles WHERE version = ?", (target_version,)
        ).fetchone()
    else:
        target = conn.execute(
            "SELECT * FROM target_profiles WHERE profile_id < ? ORDER BY profile_id DESC LIMIT 1",
            (int(active["profile_id"]),),
        ).fetchone()

    if target is None:
        return False, "되돌릴 이전 Profile이 없습니다."

    with transaction(conn):
        conn.execute("UPDATE target_profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE target_profiles SET is_active = 1 WHERE profile_id = ?", (int(target["profile_id"]),)
        )
    message = f"{active['version']} → {target['version']} 로 되돌렸습니다."
    logger.info(message)
    return True, message
