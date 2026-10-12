"""Query Bank — 검색어 은행 (Phase 19A).

고정된 소수 검색어를 반복하는 대신, 카테고리별 검색어를 은행에 모아 두고
성과를 기록한다. **매 Run에 전부 검색하지 않는다** — 선택은 Query Planner(19B)가 한다.

여기서 하는 일은 저장과 집계뿐이다. Instagram에 접속하지 않는다.
검색어 회전은 **Discovery 다양성**을 위한 것이며 탐지 회피 수단이 아니다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Sequence

from ..core.database import utc_now
from ..core.logger import get_logger

logger = get_logger("discovery.query_bank")

ORIGIN_SEED = "seed"
ORIGIN_EXPANSION = "expansion"
ORIGIN_OPERATOR = "operator"
ORIGIN_CONFIG = "config"


class QueryStatus(str, Enum):
    """검색어 상태. 영구 삭제하지 않고 DISABLED로 남긴다(이력 보존)."""

    ACTIVE = "ACTIVE"
    EXPERIMENTAL = "EXPERIMENTAL"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"

    @property
    def selectable(self) -> bool:
        """이번 Run의 후보가 될 수 있는 상태인지."""
        return self in (QueryStatus.ACTIVE, QueryStatus.EXPERIMENTAL)


# 초기 Seed — §9의 카테고리 10개. 은행에 담아 두고 매 Run 일부만 쓴다.
SEED_QUERIES: tuple[tuple[str, str], ...] = (
    # 직장생활
    ("직장인", "직장생활"),
    ("회사원", "직장생활"),
    ("회사생활", "직장생활"),
    ("직장인일상", "직장생활"),
    ("직장인브이로그", "직장생활"),
    ("사회초년생", "직장생활"),
    ("오피스룩", "직장생활"),
    ("직장인공감", "직장생활"),
    # 출퇴근
    ("출근", "출퇴근"),
    ("출근길", "출퇴근"),
    ("출근룩", "출퇴근"),
    ("퇴근", "출퇴근"),
    ("퇴근길", "출퇴근"),
    ("퇴근하는길", "출퇴근"),
    ("지하철출근", "출퇴근"),
    ("통근", "출퇴근"),
    # 일상
    ("일상브이로그", "일상"),
    ("평일일상", "일상"),
    ("소소한일상", "일상"),
    ("데일리브이로그", "일상"),
    ("하루기록", "일상"),
    ("일상기록", "일상"),
    ("브이로그", "일상"),
    # 퇴근 후
    ("퇴근후", "퇴근후"),
    ("퇴근루틴", "퇴근후"),
    ("퇴근후일상", "퇴근후"),
    ("퇴근후운동", "퇴근후"),
    ("퇴근후카페", "퇴근후"),
    ("저녁루틴", "퇴근후"),
    ("야근", "퇴근후"),
    # 주말
    ("주말일상", "주말"),
    ("주말브이로그", "주말"),
    ("주말나들이", "주말"),
    ("주말데이트", "주말"),
    ("주말요리", "주말"),
    ("토요일", "주말"),
    # 음식
    ("직장인점심", "음식"),
    ("점심메뉴", "음식"),
    ("혼밥", "음식"),
    ("저녁메뉴", "음식"),
    ("집밥", "음식"),
    ("자취요리", "음식"),
    ("간단요리", "음식"),
    ("맛집", "음식"),
    ("배달음식", "음식"),
    # 카페
    ("카페투어", "카페"),
    ("카페스타그램", "카페"),
    ("동네카페", "카페"),
    ("커피한잔", "카페"),
    ("디저트카페", "카페"),
    ("카페일상", "카페"),
    # 운동
    ("러닝", "운동"),
    ("헬스", "운동"),
    ("홈트", "운동"),
    ("아침운동", "운동"),
    ("한강러닝", "운동"),
    ("요가", "운동"),
    ("운동루틴", "운동"),
    # 취미
    ("취미생활", "취미"),
    ("그림그리기", "취미"),
    ("독서기록", "취미"),
    ("캠핑", "취미"),
    ("드라이브", "취미"),
    ("반려견일상", "취미"),
    # 자기계발
    ("자기계발", "자기계발"),
    ("공부기록", "자기계발"),
    ("미라클모닝", "자기계발"),
    ("재테크", "자기계발"),
    ("이직준비", "자기계발"),
    ("사이드프로젝트", "자기계발"),
)


@dataclass
class QueryRecord:
    """query_bank 한 행."""

    query_id: int
    query: str
    category: str
    origin: str
    status: QueryStatus
    run_count: int = 0
    last_run_at: Optional[str] = None
    found_count: int = 0
    new_count: int = 0
    duplicate_count: int = 0
    unique_creator_count: int = 0
    score_sum: float = 0.0
    score_count: int = 0
    score_75_plus: int = 0
    score_82_plus: int = 0
    utility: Optional[float] = None
    last_success_at: Optional[str] = None
    cooldown_until: Optional[str] = None
    status_reason: Optional[str] = None

    # --- 성과 비율 (표본이 없으면 None — 0으로 단정하지 않는다) ---------
    @property
    def avg_target_score(self) -> Optional[float]:
        return self.score_sum / self.score_count if self.score_count else None

    @property
    def new_rate(self) -> Optional[float]:
        total = self.new_count + self.duplicate_count
        return self.new_count / total if total else None

    @property
    def duplicate_rate(self) -> Optional[float]:
        total = self.new_count + self.duplicate_count
        return self.duplicate_count / total if total else None

    @property
    def unique_creator_rate(self) -> Optional[float]:
        return self.unique_creator_count / self.new_count if self.new_count else None

    @property
    def score_75_rate(self) -> Optional[float]:
        return self.score_75_plus / self.score_count if self.score_count else None

    @property
    def score_82_rate(self) -> Optional[float]:
        return self.score_82_plus / self.score_count if self.score_count else None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "QueryRecord":
        return cls(
            query_id=int(row["query_id"]),
            query=str(row["query"]),
            category=str(row["category"]),
            origin=str(row["origin"]),
            status=QueryStatus(str(row["status"])),
            run_count=int(row["run_count"] or 0),
            last_run_at=row["last_run_at"],
            found_count=int(row["found_count"] or 0),
            new_count=int(row["new_count"] or 0),
            duplicate_count=int(row["duplicate_count"] or 0),
            unique_creator_count=int(row["unique_creator_count"] or 0),
            score_sum=float(row["score_sum"] or 0),
            score_count=int(row["score_count"] or 0),
            score_75_plus=int(row["score_75_plus"] or 0),
            score_82_plus=int(row["score_82_plus"] or 0),
            utility=row["utility"],
            last_success_at=row["last_success_at"],
            cooldown_until=row["cooldown_until"],
            status_reason=row["status_reason"],
        )


# --- 저장 ------------------------------------------------------------------
def add_query(
    conn: sqlite3.Connection,
    query: str,
    *,
    category: str = "etc",
    origin: str = ORIGIN_SEED,
    status: QueryStatus = QueryStatus.ACTIVE,
) -> Optional[int]:
    """검색어를 추가한다. 이미 있으면 **건드리지 않고** None을 반환한다(성과 보존)."""
    text = str(query or "").strip()
    if not text:
        return None
    now = utc_now()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO query_bank "
        "(query, category, origin, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (text, category, origin, status.value, now, now),
    )
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def seed_bank(
    conn: sqlite3.Connection, seeds: Sequence[tuple[str, str]] = SEED_QUERIES
) -> int:
    """Seed 검색어를 넣는다(이미 있는 것은 그대로). 추가된 개수를 반환한다."""
    added = 0
    for query, category in seeds:
        if add_query(conn, query, category=category, origin=ORIGIN_SEED) is not None:
            added += 1
    conn.commit()
    if added:
        logger.info("Query Bank seed %d개 추가(기존 항목은 변경하지 않음).", added)
    return added


def ensure_queries(
    conn: sqlite3.Connection,
    queries: Iterable[str],
    *,
    origin: str = ORIGIN_CONFIG,
    category: str = "etc",
    status: QueryStatus = QueryStatus.ACTIVE,
) -> int:
    """설정/운영자가 지정한 검색어를 은행에 반영한다."""
    added = sum(
        1
        for query in queries
        if add_query(conn, query, category=category, origin=origin, status=status) is not None
    )
    conn.commit()
    return added


# --- 조회 ------------------------------------------------------------------
def get_query(conn: sqlite3.Connection, query: str) -> Optional[QueryRecord]:
    row = conn.execute("SELECT * FROM query_bank WHERE query = ?", (str(query).strip(),)).fetchone()
    return QueryRecord.from_row(row) if row else None


def get_query_by_id(conn: sqlite3.Connection, query_id: int) -> Optional[QueryRecord]:
    row = conn.execute(
        "SELECT * FROM query_bank WHERE query_id = ?", (int(query_id),)
    ).fetchone()
    return QueryRecord.from_row(row) if row else None


def list_queries(
    conn: sqlite3.Connection,
    *,
    statuses: Optional[Sequence[QueryStatus]] = None,
    limit: Optional[int] = None,
) -> list[QueryRecord]:
    sql = "SELECT * FROM query_bank"
    params: list[object] = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        sql += f" WHERE status IN ({placeholders})"
        params.extend(status.value for status in statuses)
    sql += " ORDER BY COALESCE(utility, -1) DESC, query_id ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [QueryRecord.from_row(row) for row in conn.execute(sql, params).fetchall()]


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM query_bank GROUP BY status").fetchall()
    counts = {status.value: 0 for status in QueryStatus}
    counts.update({str(row["status"]): int(row["n"]) for row in rows})
    return counts


# --- 갱신 ------------------------------------------------------------------
def set_status(
    conn: sqlite3.Connection,
    query_id: int,
    status: QueryStatus,
    *,
    reason: str = "",
    cooldown_until: Optional[str] = None,
) -> None:
    """상태를 바꾼다. 삭제는 하지 않는다(DISABLED로 남긴다)."""
    conn.execute(
        "UPDATE query_bank SET status = ?, status_reason = ?, cooldown_until = ?, updated_at = ? "
        "WHERE query_id = ?",
        (status.value, reason or None, cooldown_until, utc_now(), int(query_id)),
    )


def set_utility(conn: sqlite3.Connection, query_id: int, utility: Optional[float]) -> None:
    conn.execute(
        "UPDATE query_bank SET utility = ?, updated_at = ? WHERE query_id = ?",
        (utility, utc_now(), int(query_id)),
    )


@dataclass
class QueryRunStats:
    """검색어 1회 실행 결과(Discovery가 채운다)."""

    found: int = 0
    collected: int = 0
    new_candidates: int = 0
    duplicates: int = 0
    unique_creators: int = 0
    scores: list[float] = None  # type: ignore[assignment]
    failed: bool = False
    failed_stage: Optional[str] = None

    def __post_init__(self) -> None:
        if self.scores is None:
            self.scores = []


def record_query_run(
    conn: sqlite3.Connection,
    query_id: int,
    stats: QueryRunStats,
    *,
    run_id: Optional[str] = None,
    at: Optional[str] = None,
) -> None:
    """실행 결과를 query_runs에 남기고 query_bank 누적치를 갱신한다."""
    now = at or utc_now()
    scores = [float(score) for score in (stats.scores or []) if score is not None]
    conn.execute(
        "INSERT INTO query_runs (query_id, run_id, started_at, found, collected, "
        "new_candidates, duplicates, unique_creators, failed, failed_stage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(query_id),
            run_id,
            now,
            stats.found,
            stats.collected,
            stats.new_candidates,
            stats.duplicates,
            stats.unique_creators,
            1 if stats.failed else 0,
            stats.failed_stage,
        ),
    )
    conn.execute(
        "UPDATE query_bank SET "
        "  run_count = run_count + 1, last_run_at = ?, "
        "  found_count = found_count + ?, new_count = new_count + ?, "
        "  duplicate_count = duplicate_count + ?, "
        "  unique_creator_count = unique_creator_count + ?, "
        "  score_sum = score_sum + ?, score_count = score_count + ?, "
        "  score_75_plus = score_75_plus + ?, score_82_plus = score_82_plus + ?, "
        "  last_success_at = CASE WHEN ? > 0 THEN ? ELSE last_success_at END, "
        "  updated_at = ? "
        "WHERE query_id = ?",
        (
            now,
            stats.found,
            stats.new_candidates,
            stats.duplicates,
            stats.unique_creators,
            sum(scores),
            len(scores),
            sum(1 for score in scores if score >= 75),
            sum(1 for score in scores if score >= 82),
            stats.new_candidates,
            now,
            now,
            int(query_id),
        ),
    )
    conn.commit()


def note_candidate_query(
    conn: sqlite3.Connection, media_pk: int, query_id: int, *, at: Optional[str] = None
) -> None:
    """후보가 어떤 검색어에서 발견됐는지 기록한다(같은 후보 + 같은 검색어는 횟수만 올린다)."""
    now = at or utc_now()
    conn.execute(
        "INSERT INTO candidate_queries (media_pk, query_id, first_found_at, last_found_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(media_pk, query_id) DO UPDATE SET "
        "  last_found_at = excluded.last_found_at, found_count = found_count + 1",
        (int(media_pk), int(query_id), now, now),
    )


def queries_found_by(conn: sqlite3.Connection, media_pk: int) -> list[str]:
    """해당 후보를 발견한 검색어 목록."""
    rows = conn.execute(
        "SELECT q.query FROM candidate_queries cq "
        "JOIN query_bank q ON q.query_id = cq.query_id "
        "WHERE cq.media_pk = ? ORDER BY cq.first_found_at, cq.query_id",
        (int(media_pk),),
    ).fetchall()
    return [str(row["query"]) for row in rows]
