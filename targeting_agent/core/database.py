"""SQLite 연결/트랜잭션 및 저장소(Repository) 헬퍼.

원칙:
- 파이프라인 각 단계는 성공했을 때만 상태를 전이한다(중간 종료 후 재실행 가능).
- 중복(동일 media / 동일 action / 동일 comment)은 DB 제약 + INSERT OR IGNORE로 차단한다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from .exceptions import DatabaseError
from .models import ActionStatus, ActionType, ContentAnalysis, MediaStatus, RawCandidate
from .schema import SCHEMA_STATEMENTS, SCHEMA_VERSION


DEFAULT_TZ_OFFSET_HOURS = 9  # 운영 기준 timezone(KST). config.actions.daily_limits.timezone_offset_hours로 변경.


def utc_now() -> str:
    """ISO8601(UTC) 타임스탬프 문자열."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_str(tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS) -> str:
    """일일 한도 판정용 날짜(기본 KST 기준 YYYY-MM-DD)."""
    from datetime import timedelta

    now = datetime.now(timezone.utc) + timedelta(hours=tz_offset_hours)
    return now.strftime("%Y-%m-%d")


def get_connection(db_path: Union[Path, str]) -> sqlite3.Connection:
    """SQLite 연결을 생성한다(폴더 자동 생성, FK 활성화)."""
    if str(db_path) != ":memory:":
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db_path = str(path)
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:  # pragma: no cover - 방어적 처리
        raise DatabaseError(f"SQLite 연결 실패: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL" if db_path != ":memory:" else "PRAGMA synchronous = OFF")
    return conn


NEW_COLUMNS_V2 = (
    ("candidate_media", "canonical_url", "TEXT"),
    ("candidate_media", "instagram_media_id", "TEXT"),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """기존 DB를 지우지 않고 필요한 컬럼만 덧붙인다(Phase 12A: schema v2)."""
    for table, column, column_type in NEW_COLUMNS_V2:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:  # 테이블 자체가 없으면 CREATE가 처리한다
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def init_db(conn: sqlite3.Connection) -> None:
    """스키마를 생성하고 schema_version을 기록한다."""
    _apply_migrations(conn)
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES ('schema_version', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (str(SCHEMA_VERSION), utc_now()),
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """블록 전체가 성공해야 commit, 예외 시 rollback."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- app_state ----------------------------------------------------------
def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utc_now()),
    )


def get_state(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# --- creators / candidates ---------------------------------------------
def upsert_creator(conn: sqlite3.Connection, candidate: RawCandidate) -> int:
    """username 기준으로 creator를 upsert하고 creator_id를 반환한다."""
    now = utc_now()
    conn.execute(
        """
        INSERT INTO creators (username, followers, is_private, language, first_seen_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            followers = MAX(excluded.followers, creators.followers),
            is_private = excluded.is_private,
            language = COALESCE(excluded.language, creators.language),
            updated_at = excluded.updated_at
        """,
        (
            candidate.username,
            int(candidate.followers),
            int(candidate.is_private),
            candidate.language,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT creator_id FROM creators WHERE username = ?", (candidate.username,)
    ).fetchone()
    return int(row["creator_id"])


def find_candidate(conn: sqlite3.Connection, candidate: RawCandidate) -> Optional[int]:
    """이미 저장된 후보인지 확인한다.

    식별 우선순위: instagram_media_id → canonical_url → media_id
    (URL만 입력된 후보는 Instagram media_id를 알 수 없으므로 canonical_url이 키가 된다.)
    """
    checks = (
        ("instagram_media_id", candidate.instagram_media_id),
        ("canonical_url", candidate.canonical_url),
        ("media_id", candidate.media_id),
    )
    for column, value in checks:
        if not value:
            continue
        row = conn.execute(
            f"SELECT media_pk FROM candidate_media WHERE {column} = ?", (value,)
        ).fetchone()
        if row:
            return int(row["media_pk"])
    return None


def insert_candidate(
    conn: sqlite3.Connection,
    candidate: RawCandidate,
    status: MediaStatus = MediaStatus.NEW,
) -> Optional[int]:
    """새 후보를 저장한다. 이미 존재하는 후보면 None을 반환한다(중복 차단)."""
    if find_candidate(conn, candidate) is not None:
        return None
    creator_id = upsert_creator(conn, candidate)
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO candidate_media (
            media_id, creator_id, permalink, canonical_url, instagram_media_id,
            media_type, caption, hashtags,
            like_count, comment_count, posted_at, language, is_ad, source,
            status, discovered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate.media_id,
            creator_id,
            candidate.permalink,
            candidate.canonical_url,
            candidate.instagram_media_id,
            candidate.media_type,
            candidate.caption,
            json.dumps(candidate.hashtags, ensure_ascii=False),
            int(candidate.like_count),
            int(candidate.comment_count),
            candidate.posted_at,
            candidate.language,
            int(candidate.is_ad),
            candidate.source,
            status.value,
            now,
            now,
        ),
    )
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def record_import_event(
    conn: sqlite3.Connection,
    *,
    source: str,
    result: str,
    original_url: str = "",
    canonical_url: Optional[str] = None,
    media_pk: Optional[int] = None,
    source_file: Optional[str] = None,
    source_row: Optional[int] = None,
    error_message: Optional[str] = None,
) -> int:
    """입력 1건의 처리 결과를 남긴다(ADDED/DUPLICATE/INVALID/ERROR)."""
    cursor = conn.execute(
        "INSERT INTO import_events (media_pk, source, source_file, source_row, "
        "original_url, canonical_url, result, error_message, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            media_pk,
            source,
            source_file,
            source_row,
            original_url,
            canonical_url,
            result,
            error_message,
            utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def fetch_candidates(
    conn: sqlite3.Connection, statuses: Iterable[str], limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """상태로 후보를 조회한다(creator 정보 join)."""
    status_list = list(statuses)
    placeholders = ",".join("?" for _ in status_list)
    sql = (
        "SELECT m.*, c.username, c.followers, c.is_private, c.last_interacted_at "
        "FROM candidate_media m JOIN creators c ON c.creator_id = m.creator_id "
        f"WHERE m.status IN ({placeholders}) "
        "ORDER BY COALESCE(m.target_score, 0) DESC, m.media_pk ASC"
    )
    params: list[Any] = list(status_list)
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return list(conn.execute(sql, params).fetchall())


def update_candidate_status(
    conn: sqlite3.Connection,
    media_pk: int,
    status: MediaStatus,
    reason: str = "",
    target_score: Optional[float] = None,
) -> None:
    conn.execute(
        "UPDATE candidate_media SET status = ?, status_reason = ?, "
        "target_score = COALESCE(?, target_score), updated_at = ? WHERE media_pk = ?",
        (status.value, reason, target_score, utc_now(), media_pk),
    )


def reset_skipped_for_rescore(conn: sqlite3.Connection) -> int:
    """점수 미달로 SKIPPED된 후보를 NEW로 되돌린다(학습 반영 후 재채점용).

    이미 Interaction이 있는 후보와 BLOCKED(광고/민감/회피 키워드) 후보는 건드리지 않는다.
    """
    cursor = conn.execute(
        "UPDATE candidate_media SET status = ?, status_reason = 'rescore', updated_at = ? "
        "WHERE status = ? AND media_pk NOT IN (SELECT media_pk FROM interactions)",
        (MediaStatus.NEW.value, utc_now(), MediaStatus.SKIPPED.value),
    )
    conn.commit()
    return int(cursor.rowcount)


# --- analysis cache ------------------------------------------------------
def get_cached_analysis(
    conn: sqlite3.Connection,
    media_pk: int,
    analyzer: str,
    analyzer_version: str,
    prompt_version: str,
) -> Optional[sqlite3.Row]:
    """동일 analyzer/version 조합의 분석 결과를 재사용한다(비용 절감)."""
    return conn.execute(
        "SELECT * FROM media_analysis WHERE media_pk = ? AND analyzer = ? "
        "AND analyzer_version = ? AND prompt_version = ?",
        (media_pk, analyzer, analyzer_version, prompt_version),
    ).fetchone()


def save_analysis(
    conn: sqlite3.Connection,
    media_pk: int,
    analysis: ContentAnalysis,
    similarity: Optional[float] = None,
    target_score: Optional[float] = None,
    score_breakdown: Optional[dict[str, Any]] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO media_analysis (
            media_pk, analyzer, analyzer_version, prompt_version, payload,
            similarity, target_score, score_breakdown, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(media_pk, analyzer, analyzer_version, prompt_version) DO UPDATE SET
            payload = excluded.payload,
            similarity = excluded.similarity,
            target_score = excluded.target_score,
            score_breakdown = excluded.score_breakdown,
            analyzed_at = excluded.analyzed_at
        """,
        (
            media_pk,
            analysis.analyzer,
            analysis.analyzer_version,
            analysis.prompt_version,
            json.dumps(analysis.to_dict(), ensure_ascii=False),
            similarity,
            target_score,
            json.dumps(score_breakdown or {}, ensure_ascii=False),
            utc_now(),
        ),
    )


# --- comment drafts ------------------------------------------------------
def save_comment_draft(
    conn: sqlite3.Connection,
    media_pk: int,
    text: str,
    normalized_text: str,
    *,
    language: str,
    quality_ok: bool,
    quality_reason: str,
    similarity_max: float,
    status: str,
    generator: str,
    generator_version: str,
) -> Optional[int]:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO comment_drafts (
            media_pk, text, normalized_text, language, length, quality_ok,
            quality_reason, similarity_max, status, generator, generator_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            media_pk,
            text,
            normalized_text,
            language,
            len(text),
            int(quality_ok),
            quality_reason,
            float(similarity_max),
            status,
            generator,
            generator_version,
            utc_now(),
        ),
    )
    return int(cursor.lastrowid) if cursor.rowcount else None


def recent_comment_texts(conn: sqlite3.Connection, limit: int = 300) -> list[str]:
    """중복 비교 대상: 저장된 댓글 후보 + 실제 사용한 댓글."""
    rows = conn.execute(
        "SELECT text FROM comment_drafts ORDER BY draft_id DESC LIMIT ?", (limit,)
    ).fetchall()
    used = conn.execute(
        "SELECT comment_text AS text FROM interactions WHERE comment_text IS NOT NULL "
        "ORDER BY interaction_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["text"] for r in list(rows) + list(used) if r["text"]]


# --- action queue / interactions ----------------------------------------
def enqueue_action(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    media_pk: int,
    creator_id: int,
    action_type: ActionType,
    target_score: float,
    priority: int,
    executor: str,
    dry_run: bool,
    draft_id: Optional[int] = None,
    comment_text: Optional[str] = None,
) -> Optional[int]:
    """Action을 큐에 넣는다. 동일 media+action_type이 이미 있으면 None."""
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO action_queue (
            run_id, media_pk, creator_id, draft_id, action_type, comment_text,
            target_score, priority, status, executor, dry_run, scheduled_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            media_pk,
            creator_id,
            draft_id,
            action_type.value,
            comment_text,
            float(target_score),
            int(priority),
            executor,
            int(dry_run),
            now,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid) if cursor.rowcount else None


def fetch_pending_actions(conn: sqlite3.Connection, limit: Optional[int] = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT a.*, m.media_id, m.permalink, c.username "
        "FROM action_queue a "
        "JOIN candidate_media m ON m.media_pk = a.media_pk "
        "JOIN creators c ON c.creator_id = a.creator_id "
        # APPROVED는 '운영자 확인 대기' 상태이므로 재실행 대상이 아니다.
        "WHERE a.status = 'PENDING' "
        "ORDER BY a.priority DESC, a.target_score DESC, a.action_id ASC"
    )
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return list(conn.execute(sql, params).fetchall())


def update_action_status(
    conn: sqlite3.Connection,
    action_id: int,
    status: ActionStatus,
    *,
    result: str = "",
    error: Optional[str] = None,
    started: bool = False,
    finished: bool = False,
) -> None:
    now = utc_now()
    conn.execute(
        "UPDATE action_queue SET status = ?, result = ?, error = ?, "
        "started_at = CASE WHEN ? THEN ? ELSE started_at END, "
        "finished_at = CASE WHEN ? THEN ? ELSE finished_at END, "
        "updated_at = ? WHERE action_id = ?",
        (status.value, result, error, int(started), now, int(finished), now, now, action_id),
    )


def record_interaction(
    conn: sqlite3.Connection,
    *,
    media_pk: int,
    creator_id: int,
    action_id: Optional[int],
    action_type: ActionType,
    executor: str,
    dry_run: bool,
    success: bool,
    comment_text: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[int]:
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO interactions (
            media_pk, creator_id, action_id, action_type, comment_text,
            executor, dry_run, success, error, executed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            media_pk,
            creator_id,
            action_id,
            action_type.value,
            comment_text,
            executor,
            int(dry_run),
            int(success),
            error,
            now,
        ),
    )
    if cursor.rowcount and success:
        conn.execute(
            "UPDATE creators SET last_interacted_at = ?, updated_at = ? WHERE creator_id = ?",
            (now, now, creator_id),
        )
    return int(cursor.lastrowid) if cursor.rowcount else None


def has_interaction(
    conn: sqlite3.Connection,
    media_pk: int,
    dry_run: Optional[bool] = None,
    action_type: Optional[ActionType] = None,
) -> bool:
    """해당 media에 이미 Interaction이 있었는지 확인한다.

    action_type을 주면 같은 종류의 Action만 본다. 같은 게시물에 LIKE를 한 뒤
    COMMENT를 다는 것은 설계상 정상 흐름이므로, 종류를 구분해야 한다.
    """
    sql = "SELECT 1 FROM interactions WHERE media_pk = ?"
    params: list[Any] = [media_pk]
    if dry_run is not None:
        sql += " AND dry_run = ?"
        params.append(int(dry_run))
    if action_type is not None:
        sql += " AND action_type = ?"
        params.append(action_type.value)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def _local_date_expr(tz_offset_hours: int, column: str = "executed_at") -> str:
    """UTC로 저장된 타임스탬프를 운영 timezone 기준 날짜로 변환하는 SQL 식."""
    return f"date({column}, '{tz_offset_hours:+d} hours')"


def count_actions_today(
    conn: sqlite3.Connection,
    action_type: ActionType,
    date_str: str,
    dry_run: bool,
    tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS,
) -> int:
    """오늘(운영 timezone 기준) 성공 처리한 action 수. dry_run 여부를 분리 집계한다."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM interactions "
        "WHERE action_type = ? AND success = 1 AND dry_run = ? "
        f"AND {_local_date_expr(tz_offset_hours)} = ?",
        (action_type.value, int(dry_run), date_str),
    ).fetchone()
    return int(row["n"])


def count_creator_actions_today(
    conn: sqlite3.Connection,
    creator_id: int,
    date_str: str,
    dry_run: bool,
    tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS,
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM interactions "
        "WHERE creator_id = ? AND success = 1 AND dry_run = ? "
        f"AND {_local_date_expr(tz_offset_hours)} = ?",
        (creator_id, int(dry_run), date_str),
    ).fetchone()
    return int(row["n"])


def count_awaiting_today(
    conn: sqlite3.Connection,
    action_type: ActionType,
    date_str: str,
    dry_run: bool,
    tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS,
) -> int:
    """오늘 목록에 올라가 운영자 확인을 기다리는 Action 수.

    아직 Interaction으로 기록되지 않았지만 곧 실제로 수행될 건이므로
    일일 한도 계산에 포함해야 한도를 초과한 목록이 만들어지지 않는다.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM action_queue "
        "WHERE action_type = ? AND status = 'APPROVED' AND dry_run = ? "
        f"AND {_local_date_expr(tz_offset_hours, 'updated_at')} = ?",
        (action_type.value, int(dry_run), date_str),
    ).fetchone()
    return int(row["n"])


def fetch_awaiting_actions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """운영자 확인 대기(APPROVED) Action 목록."""
    return list(
        conn.execute(
            "SELECT a.*, m.media_id, m.permalink, c.username "
            "FROM action_queue a "
            "JOIN candidate_media m ON m.media_pk = a.media_pk "
            "JOIN creators c ON c.creator_id = a.creator_id "
            "WHERE a.status = 'APPROVED' "
            "ORDER BY a.priority DESC, a.action_id ASC"
        ).fetchall()
    )


def count_creator_media_today(
    conn: sqlite3.Connection,
    creator_id: int,
    date_str: str,
    dry_run: bool,
    tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS,
) -> int:
    """오늘 이 Creator의 게시물 중 몇 건에 Interaction 했는지(게시물 기준, 중복 제외)."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT media_pk) AS n FROM interactions "
        "WHERE creator_id = ? AND success = 1 AND dry_run = ? "
        f"AND {_local_date_expr(tz_offset_hours)} = ?",
        (creator_id, int(dry_run), date_str),
    ).fetchone()
    return int(row["n"])


def last_creator_interaction_at(
    conn: sqlite3.Connection, creator_id: int, dry_run: bool
) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(executed_at) AS ts FROM interactions "
        "WHERE creator_id = ? AND success = 1 AND dry_run = ?",
        (creator_id, int(dry_run)),
    ).fetchone()
    return row["ts"] if row and row["ts"] else None


# --- daily stats ---------------------------------------------------------
def bump_stat(conn: sqlite3.Connection, date_str: str, metric: str, delta: int = 1) -> None:
    conn.execute(
        "INSERT INTO daily_stats (stat_date, metric, value, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(stat_date, metric) DO UPDATE SET "
        "value = daily_stats.value + excluded.value, updated_at = excluded.updated_at",
        (date_str, metric, int(delta), utc_now()),
    )


def get_stats(conn: sqlite3.Connection, date_str: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT metric, value FROM daily_stats WHERE stat_date = ?", (date_str,)
    ).fetchall()
    return {row["metric"]: int(row["value"]) for row in rows}
