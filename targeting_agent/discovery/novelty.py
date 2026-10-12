"""Novelty Engine — 반복 발견 억제 (Phase 19D).

같은 Reel이 여러 검색어에서 발견되는 것은 정상이다. 문제는 그때마다
다시 분석하고 다시 Action을 만드는 것이다. 그래서 반복 발견은
**metadata만 갱신**하고 Claude 호출과 Action 생성은 하지 않는다.

중복 판정은 Phase 12A(instagram_media_id → canonical_url → media_id)와
기존 Interaction/Creator cooldown을 그대로 쓴다. 별도 중복 시스템을 만들지 않는다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..core.database import utc_now
from ..core.logger import get_logger
from .query_bank import note_candidate_query, queries_found_by

logger = get_logger("discovery.novelty")


@dataclass
class NoveltyStats:
    """Run 단위 Novelty 집계."""

    new_candidates: int = 0
    repeat_candidates: int = 0
    claude_calls_saved: int = 0  # 반복 발견이라 분석을 건너뛴 건수
    queries_attributed: int = 0
    repeat_queries: dict[str, int] = field(default_factory=dict)

    def note_repeat(self, query: str) -> None:
        self.repeat_candidates += 1
        self.claude_calls_saved += 1
        self.repeat_queries[query] = self.repeat_queries.get(query, 0) + 1

    @property
    def repeat_rate(self) -> Optional[float]:
        total = self.new_candidates + self.repeat_candidates
        return self.repeat_candidates / total if total else None


def mark_seen(
    conn: sqlite3.Connection,
    media_pk: int,
    *,
    first_time: bool,
    at: Optional[str] = None,
) -> None:
    """후보의 발견 이력을 갱신한다(새 후보면 first_seen도 채운다)."""
    now = at or utc_now()
    if first_time:
        conn.execute(
            "UPDATE candidate_media SET first_seen_at = COALESCE(first_seen_at, ?), "
            "last_seen_at = ?, seen_count = 1 WHERE media_pk = ?",
            (now, now, int(media_pk)),
        )
    else:
        conn.execute(
            "UPDATE candidate_media SET last_seen_at = ?, "
            "seen_count = COALESCE(seen_count, 1) + 1 WHERE media_pk = ?",
            (now, int(media_pk)),
        )


def attribute(
    conn: sqlite3.Connection,
    media_pk: int,
    query_id: Optional[int],
    *,
    at: Optional[str] = None,
) -> None:
    """후보를 발견한 검색어를 기록한다. 같은 후보라도 검색어별로 남는다."""
    if query_id is None:
        return
    note_candidate_query(conn, media_pk, query_id, at=at)


def record_discovery(
    conn: sqlite3.Connection,
    media_pk: int,
    *,
    query: str,
    query_id: Optional[int],
    is_new: bool,
    stats: Optional[NoveltyStats] = None,
    at: Optional[str] = None,
) -> None:
    """Discovery 1건의 Novelty 처리(새 후보 / 반복 발견 공통 입구)."""
    mark_seen(conn, media_pk, first_time=is_new, at=at)
    attribute(conn, media_pk, query_id, at=at)
    if stats is not None:
        if is_new:
            stats.new_candidates += 1
        else:
            stats.note_repeat(query)
        if query_id is not None:
            stats.queries_attributed += 1


@dataclass
class CandidateNovelty:
    """후보 1건의 Novelty 정보(Dashboard/리포트용)."""

    media_pk: int
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    seen_count: int
    queries: list[str] = field(default_factory=list)

    @property
    def is_repeat(self) -> bool:
        return self.seen_count > 1


def candidate_novelty(conn: sqlite3.Connection, media_pk: int) -> Optional[CandidateNovelty]:
    row = conn.execute(
        "SELECT media_pk, first_seen_at, last_seen_at, COALESCE(seen_count, 1) AS seen_count "
        "FROM candidate_media WHERE media_pk = ?",
        (int(media_pk),),
    ).fetchone()
    if row is None:
        return None
    return CandidateNovelty(
        media_pk=int(row["media_pk"]),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        seen_count=int(row["seen_count"] or 1),
        queries=queries_found_by(conn, media_pk),
    )


def repeat_summary(conn: sqlite3.Connection) -> dict[str, object]:
    """반복 발견 현황(모니터링용)."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN COALESCE(seen_count, 1) > 1 THEN 1 ELSE 0 END) AS repeats, "
        "       MAX(COALESCE(seen_count, 1)) AS max_seen "
        "FROM candidate_media"
    ).fetchone()
    total = int(row["total"] or 0)
    repeats = int(row["repeats"] or 0)
    multi = conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT media_pk FROM candidate_queries GROUP BY media_pk HAVING COUNT(*) > 1"
        ")"
    ).fetchone()
    return {
        "candidates": total,
        "repeat_candidates": repeats,
        "repeat_rate": (repeats / total) if total else None,
        "max_seen_count": int(row["max_seen"] or 0),
        "multi_query_candidates": int(multi["n"] or 0),
    }
