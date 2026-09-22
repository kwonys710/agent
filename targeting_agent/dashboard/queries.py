"""Dashboard 조회 전용 SQL (Phase 14).

읽기만 한다. Claude를 호출하지 않고 DB에 저장된 분석 결과만 사용한다.
전체 테이블을 메모리에 올리지 않고 항상 조건과 LIMIT을 건다.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Optional, Sequence

from dataclasses import dataclass

from ..core.database import count_by_final_analyzer, today_str
from ..core.models import ActionType, MediaStatus

# 검토 화면에 올릴 후보 상태(처리 완료/차단은 기본 제외)
REVIEW_STATUSES = (
    MediaStatus.SCORED.value,
    MediaStatus.ANALYZED.value,
    MediaStatus.NEW.value,
    MediaStatus.NEEDS_ENRICHMENT.value,
)
DEFAULT_LIMIT = 50


def _analysis_join() -> str:
    """후보별 최신 분석 1건을 붙인다(Claude 결과 우선)."""
    return (
        "LEFT JOIN media_analysis a ON a.analysis_id = ("
        "  SELECT analysis_id FROM media_analysis x WHERE x.media_pk = m.media_pk "
        "  ORDER BY (x.analyzer = 'claude_code') DESC, x.analyzed_at DESC LIMIT 1)"
    )


def daily_summary(conn: sqlite3.Connection, tz_offset: int = 9) -> dict[str, Any]:
    """상단 요약. 저장된 통계와 상태 집계만 사용한다(모두 같은 기준일)."""
    date = today_str(tz_offset)
    statuses = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM candidate_media GROUP BY status"
        )
    }
    stats = {
        row["metric"]: int(row["value"])
        for row in conn.execute(
            "SELECT metric, value FROM daily_stats WHERE stat_date = ?", (date,)
        )
    }
    queue = {
        f"{row['action_type']}:{row['status']}": int(row["n"])
        for row in conn.execute(
            "SELECT action_type, status, COUNT(*) AS n FROM action_queue GROUP BY 1, 2"
        )
    }
    # 후보별 최종 분석 방식 기준(같은 후보가 Claude/Heuristic 양쪽에 잡히지 않는다).
    # 기준일은 상단 카드의 다른 값과 동일하게 '오늘 분석된 후보'로 맞춘다.
    analyzers = count_by_final_analyzer(conn, date, tz_offset)
    return {
        "date": date,
        "discovered": stats.get("discovered", 0),
        # '분석 완료'도 Claude/Heuristic 카드와 같은 기준(오늘 분석된 후보 수)으로 센다.
        # daily_stats 카운터는 파이프라인 실행에서만 증가해 Dashboard 분석분이 빠졌었다.
        "analyzed": analyzers.get("claude_code", 0) + analyzers.get("heuristic", 0),
        "pending_review": sum(statuses.get(s, 0) for s in REVIEW_STATUSES),
        "approved": statuses.get(MediaStatus.QUEUED.value, 0),
        "skipped": statuses.get(MediaStatus.SKIPPED.value, 0),
        "interacted": statuses.get(MediaStatus.INTERACTED.value, 0),
        "like_queue": queue.get("LIKE:PENDING", 0),
        "comment_queue": queue.get("COMMENT:PENDING", 0),
        "claude_calls": stats.get("claude_requests", 0),
        "claude_analyzed": analyzers.get("claude_code", 0),
        "heuristic_analyzed": analyzers.get("heuristic", 0),
    }


def daily_action_usage(
    conn: sqlite3.Connection, tz_offset: int = 9, dry_run: bool = True
) -> dict[str, int]:
    """오늘 사용한 LIKE/COMMENT 수(Rate Limiter와 같은 기준으로 집계)."""
    from ..core.database import count_actions_today, count_awaiting_today

    date = today_str(tz_offset)
    usage: dict[str, int] = {}
    for action_type in (ActionType.LIKE, ActionType.COMMENT):
        done = count_actions_today(conn, action_type, date, dry_run, tz_offset)
        awaiting = count_awaiting_today(conn, action_type, date, dry_run, tz_offset)
        usage[action_type.value] = done + awaiting
    return usage


def list_candidates(
    conn: sqlite3.Connection,
    *,
    statuses: Optional[Sequence[str]] = None,
    source: Optional[str] = None,
    analyzer: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    limit: int = DEFAULT_LIMIT,
) -> list[sqlite3.Row]:
    """검토 목록. Target Score 내림차순."""
    conditions: list[str] = []
    params: list[Any] = []

    status_list = list(statuses) if statuses else list(REVIEW_STATUSES)
    conditions.append(f"m.status IN ({','.join('?' for _ in status_list)})")
    params += status_list

    if source:
        conditions.append("m.source LIKE ?")
        params.append(f"{source}%")
    if analyzer:
        conditions.append("COALESCE(a.analyzer, 'none') = ?")
        params.append(analyzer)
    if min_score is not None:
        conditions.append("COALESCE(m.target_score, 0) >= ?")
        params.append(float(min_score))
    if max_score is not None:
        conditions.append("COALESCE(m.target_score, 0) <= ?")
        params.append(float(max_score))

    sql = (
        "SELECT m.media_pk, m.media_id, m.permalink, m.caption, m.source, m.status, "
        "m.target_score, c.username, a.analyzer, a.payload "
        "FROM candidate_media m JOIN creators c ON c.creator_id = m.creator_id "
        f"{_analysis_join()} WHERE {' AND '.join(conditions)} "
        "ORDER BY COALESCE(m.target_score, 0) DESC, m.media_pk DESC LIMIT ?"
    )
    params.append(int(limit))
    return list(conn.execute(sql, params).fetchall())


def candidate_detail(conn: sqlite3.Connection, media_pk: int) -> Optional[dict[str, Any]]:
    """상세 화면 데이터(분석 payload/점수 구성/댓글/Interaction 포함)."""
    row = conn.execute(
        "SELECT m.*, c.username, c.followers, c.is_private, c.last_interacted_at, "
        "a.analyzer, a.analyzer_version, a.prompt_version, a.payload, a.similarity, "
        "a.score_breakdown, a.analyzed_at "
        "FROM candidate_media m JOIN creators c ON c.creator_id = m.creator_id "
        f"{_analysis_join()} WHERE m.media_pk = ?",
        (media_pk,),
    ).fetchone()
    if row is None:
        return None

    detail = dict(row)
    detail["analysis"] = _loads(row["payload"])
    detail["breakdown"] = _loads(row["score_breakdown"])
    detail["hashtags"] = _loads(row["hashtags"], default=[])
    detail["comments"] = list(
        conn.execute(
            "SELECT draft_id, text, status, quality_ok, quality_reason, generator, created_at "
            "FROM comment_drafts WHERE media_pk = ? AND quality_ok = 1 "
            "ORDER BY (status = 'SELECTED') DESC, draft_id ASC LIMIT 10",
            (media_pk,),
        ).fetchall()
    )
    detail["actions"] = list(
        conn.execute(
            "SELECT action_id, action_type, status, comment_text, approved_at, updated_at "
            "FROM action_queue WHERE media_pk = ? ORDER BY action_id",
            (media_pk,),
        ).fetchall()
    )
    detail["interactions"] = list(
        conn.execute(
            "SELECT action_type, dry_run, success, executed_at FROM interactions "
            "WHERE media_pk = ? ORDER BY interaction_id DESC LIMIT 10",
            (media_pk,),
        ).fetchall()
    )
    detail["feedback"] = [
        row["feedback_type"]
        for row in conn.execute(
            "SELECT DISTINCT feedback_type FROM feedback WHERE media_pk = ?", (media_pk,)
        )
    ]
    return detail


def list_actions(conn: sqlite3.Connection, limit: int = DEFAULT_LIMIT) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT q.action_id, q.action_type, q.status, q.comment_text, q.target_score, "
            "q.approved_at, q.created_at, q.updated_at, c.username, m.permalink "
            "FROM action_queue q JOIN creators c ON c.creator_id = q.creator_id "
            "JOIN candidate_media m ON m.media_pk = q.media_pk "
            "ORDER BY q.action_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    )


def feedback_summary(conn: sqlite3.Connection, limit: int = 20) -> dict[str, Any]:
    counts = {
        row["feedback_type"]: int(row["n"])
        for row in conn.execute(
            "SELECT feedback_type, COUNT(*) AS n FROM feedback GROUP BY feedback_type"
        )
    }
    recent = list(
        conn.execute(
            "SELECT f.feedback_type, f.created_at, f.note, c.username "
            "FROM feedback f LEFT JOIN creators c ON c.creator_id = f.creator_id "
            "ORDER BY f.feedback_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    )
    return {"counts": counts, "recent": recent}


def learning_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """Dashboard Learning 섹션용 요약(계산은 저장된 데이터만 사용)."""
    from ..learning.profiles import get_active_profile

    active = get_active_profile(conn)
    last = conn.execute(
        "SELECT started_at, feedback_count, changed_topics, status, dry_run, new_profile "
        "FROM learning_runs ORDER BY learning_run_id DESC LIMIT 1"
    ).fetchone()
    feedback_count = int(
        conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    )
    changes: list[str] = []
    if last is not None and last["changed_topics"]:
        try:
            changes = [str(item) for item in json.loads(last["changed_topics"])]
        except (json.JSONDecodeError, TypeError):
            changes = []
    return {
        "version": active.version,
        "weights": active.topic_weights,
        "feedback_count": feedback_count,
        "last_run_at": last["started_at"] if last else "",
        "last_status": last["status"] if last else "",
        "last_applied": bool(last and not last["dry_run"]),
        "changes": changes,
    }


@dataclass(frozen=True)
class ActionDefaults:
    """Candidate Detail의 LIKE/COMMENT 기본 체크 상태와 안내 문구."""

    like: bool
    comment: bool
    notice: str
    source: str  # threshold | queue | skipped


def default_action_selection(detail: Mapping[str, Any], config: Any) -> ActionDefaults:
    """현재 Target Score와 config 기준으로 기본 체크 상태를 정한다.

    우선순위:
      1. 이미 만들어진 Action Queue(운영자의 이전 선택) — 새 기본값이 덮어쓰지 않는다
      2. SKIPPED 후보 — 기본은 모두 해제
      3. Target Score와 임계값 비교

    임계값은 **추천 기준**이다. 미달이어도 사용자가 직접 체크할 수 있다(차단하지 않는다).
    """
    like_threshold = float(config.get("actions.require_score_for_like", 75))
    comment_threshold = float(config.get("actions.require_score_for_comment", 82))
    score = detail.get("target_score")
    score_value = float(score) if score is not None else 0.0
    score_text = "-" if score is None else f"{score_value:.0f}"

    live = {
        str(row["action_type"])
        for row in (detail.get("actions") or [])
        if str(row["status"]) in ("PENDING", "APPROVED", "RUNNING", "SUCCESS")
    }
    if live:
        return ActionDefaults(
            like="LIKE" in live,
            comment="COMMENT" in live,
            notice="이미 만들어진 Action 선택을 유지합니다.",
            source="queue",
        )

    if str(detail.get("status") or "") == MediaStatus.SKIPPED.value:
        return ActionDefaults(
            like=False,
            comment=False,
            notice=f"보류(Skip)한 후보입니다. 현재 Score {score_text}",
            source="skipped",
        )

    like_ok = score is not None and score_value >= like_threshold
    comment_ok = score is not None and score_value >= comment_threshold
    parts = [
        f"현재 Score {score_text}",
        f"LIKE 추천 기준 {like_threshold:.0f} {'충족' if like_ok else '미달'}",
        f"COMMENT 추천 기준 {comment_threshold:.0f} {'충족' if comment_ok else '미달'}",
    ]
    if not like_ok and not comment_ok:
        parts.append("자동 추천 기준 미달 — 필요하면 직접 선택할 수 있습니다")
    return ActionDefaults(like=like_ok, comment=comment_ok, notice=" · ".join(parts), source="threshold")


def analyzer_label(analyzer: Optional[str]) -> str:
    """분석 방식 표시용 라벨."""
    if analyzer == "claude_code":
        return "Claude"
    if analyzer == "heuristic":
        return "Heuristic"
    return "분석 대기"


def _loads(value: Any, default: Any = None) -> Any:
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {} if default is None else default
