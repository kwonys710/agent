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


def operational_status(conn: sqlite3.Connection, config: Any) -> dict[str, Any]:
    """상단 운영 상태(Phase 18C.1).

    Discovery / Claude / Executor / Autopilot / 마지막 실행만 보여 준다.
    기존 화면을 재설계하지 않고 한 줄 상태만 덧붙이기 위한 최소 조회다.
    """
    from ..actions.policy import RunMode, usage_note

    mode = RunMode.from_config(config)
    executor_mode = str(config.get("browser_executor.mode", "DRY_RUN")).upper()
    discovery_enabled = bool(config.get("browser_discovery.enabled", False))

    def last(table: str, columns: str) -> Optional[sqlite3.Row]:
        try:
            return conn.execute(
                f"SELECT {columns} FROM {table} ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:  # pragma: no cover - 테이블이 아직 없는 경우
            return None

    discovery_row = last("browser_discovery_runs", "status, started_at, added, found")
    autopilot_row = last("autopilot_runs", "status, mode, started_at, real_writes")

    return {
        "mode": mode.value,
        "mode_label": "운영자 승인(REVIEW)" if mode is RunMode.REVIEW else "자동 승인(AUTOPILOT)",
        "executor_mode": executor_mode,
        "executor_label": {
            "DISABLED": "실행 안 함",
            "DRY_RUN": "실제 동작 없음(Dry Run)",
            "LIVE": "실제 좋아요/댓글 수행",
        }.get(executor_mode, executor_mode),
        "dry_run": bool(config.dry_run),
        "discovery_enabled": discovery_enabled,
        "discovery_last": (
            f"{discovery_row['status']} · 신규 {discovery_row['added']} ({discovery_row['started_at'][:16]})"
            if discovery_row
            else "실행 기록 없음"
        ),
        "autopilot_last": (
            f"{autopilot_row['status']} · {autopilot_row['mode']} · 실제 쓰기 "
            f"{autopilot_row['real_writes']} ({autopilot_row['started_at'][:16]})"
            if autopilot_row
            else "실행 기록 없음"
        ),
        "ai_provider": str(config.get("ai.provider", "heuristic")),
        "usage": usage_note(config, conn),
        "awaiting_approval": int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM action_queue "
                "WHERE status = 'PENDING' AND approved_at IS NULL"
            ).fetchone()["n"]
        ),
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
    from ..actions.policy import ActionPolicy

    policy = ActionPolicy.from_config(config)
    like_threshold = policy.like_threshold
    comment_threshold = policy.comment_threshold
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

    # 추천 판단도 Action Policy를 그대로 쓴다(임계값을 Dashboard에 중복 정의하지 않는다).
    recommended = policy.recommend(
        score_value if score is not None else None, apply_minimum=False
    )
    like_ok = ActionType.LIKE in recommended
    comment_ok = ActionType.COMMENT in recommended
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


# --- Discovery Health (Phase 19K) ------------------------------------------
# 19A~19E가 이미 집계한 값을 읽어 보여 주기만 한다. 비율 정의를 여기서 중복하지 않는다.
# 표본이 없는 비율(Optional[float] = None)은 0%가 아니라 "-"로 표시한다.
def _rate_text(value: Optional[float]) -> str:
    """비율 표시. 표본이 없으면(None) 0%가 아니라 "-"."""
    return "-" if value is None else f"{value * 100:.0f}%"


def _score_text(value: Optional[float]) -> str:
    return "-" if value is None else f"{float(value):.1f}"


def score_missing(conn: sqlite3.Connection, config: Any) -> dict[str, str]:
    """입력 결측(중립값으로 채점된 축) 비율(P2-3, 읽기 전용).

    비율 정의는 readiness.calibration 한 곳에만 있고 여기서는 읽어서 포맷만 한다.
    주의: collect_calibration은 candidate_media ⋈ media_analysis **전건 스캔**이다.
    후보가 많아지면 Dashboard 렌더 비용이 늘어난다(캐시는 아직 없다).
    표본이 없으면 0%가 아니라 "-".
    """
    from ..readiness.calibration import collect_calibration

    calibration = collect_calibration(conn, config)
    rates = calibration.missing_rates()
    return {
        "creator_fit": _rate_text(rates["creator_fit"]),
        "activity": _rate_text(rates["activity"]),
        "engagement": _rate_text(rates["engagement"]),
        "coverage": _rate_text(calibration.recorded_coverage),
    }


def discovery_health(
    conn: sqlite3.Connection, *, recent_runs: int = 200, now: Any = None
) -> dict[str, Any]:
    """Query Bank / Planner / Novelty / Freshness 현황(읽기 전용).

    `recent_runs`는 집계에 쓸 최근 query_runs 개수다(기본 200건).
    """
    from ..learning.operations import (
        collect_freshness_metrics,
        collect_novelty_metrics,
        collect_query_metrics,
        query_category_distribution,
    )

    query = collect_query_metrics(conn, recent_runs=recent_runs)
    novelty = collect_novelty_metrics(conn)
    freshness = collect_freshness_metrics(conn, now=now)
    return {
        "bank_size": query.bank_size,
        "categories": query.categories,
        "status": dict(query.status),
        "never_run": query.never_run,
        "avg_utility": query.avg_utility,
        "avg_utility_text": "-" if query.avg_utility is None else f"{query.avg_utility:.2f}",
        "last_batch": list(query.last_batch),
        "runs": query.runs,
        "queries_run": query.queries_run,
        "failed_runs": query.failed_runs,
        "found": query.found,
        "collected": query.collected,
        "new_candidates": query.new_candidates,
        "duplicates": query.duplicates,
        "unique_creators": query.unique_creators,
        "new_rate": _rate_text(query.new_rate),
        "duplicate_rate": _rate_text(query.duplicate_rate),
        "unique_creator_rate": _rate_text(query.unique_creator_rate),
        "categories_run": query_category_distribution(conn),
        "novelty": {
            "candidates": novelty.candidates,
            "repeat_candidates": novelty.repeat_candidates,
            "repeat_rate": _rate_text(novelty.repeat_rate),
            "max_seen_count": novelty.max_seen_count,
            "multi_query_candidates": novelty.multi_query_candidates,
        },
        "freshness": dict(freshness.bands),
        "fresh_rate": _rate_text(freshness.fresh_rate),
    }


def query_performance(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """검색어별 성과 행(Utility 내림차순). 표본이 없는 비율은 "-"로 돌려준다."""
    from ..discovery.query_bank import list_queries

    rows: list[dict[str, Any]] = []
    for record in list_queries(conn, limit=limit):
        rows.append(
            {
                "query": record.query,
                "category": record.category,
                "status": record.status.value,
                "run_count": record.run_count,
                "found": record.found_count,
                "new": record.new_count,
                "duplicate": record.duplicate_count,
                "unique_creators": record.unique_creator_count,
                "new_rate": _rate_text(record.new_rate),
                "duplicate_rate": _rate_text(record.duplicate_rate),
                "unique_creator_rate": _rate_text(record.unique_creator_rate),
                "avg_score": _score_text(record.avg_target_score),
                "score_75_rate": _rate_text(record.score_75_rate),
                "score_82_rate": _rate_text(record.score_82_rate),
                "utility": "-" if record.utility is None else f"{float(record.utility):.2f}",
                "last_run_at": record.last_run_at or "-",
            }
        )
    return rows


def engagement_overview(conn: sqlite3.Connection, config: Any) -> dict[str, dict[str, int]]:
    """LIKE / COMMENT 준비 상태(Phase 19K).

    "eligible"은 **Action Policy의 추천 임계값**을 그대로 쓴다
    (`actions.require_score_for_like` / `require_score_for_comment`).
    Dashboard에 75/82 같은 숫자를 다시 적어 두지 않는다.
    하한(`scoring.minimum_target_score`)은 적용하지 않는다 — 운영자가 설정한
    임계값 기준으로 보여 주기 위함이다(자동 경로는 Policy가 하한을 지킨다).
    """
    from ..actions.policy import ActionPolicy

    policy = ActionPolicy.from_config(config)
    like_eligible = 0
    comment_eligible = 0
    for row in conn.execute(
        "SELECT target_score FROM candidate_media WHERE target_score IS NOT NULL"
    ).fetchall():
        recommended = policy.recommend(row["target_score"], apply_minimum=False)
        if ActionType.LIKE in recommended:
            like_eligible += 1
        if ActionType.COMMENT in recommended:
            comment_eligible += 1

    def queue_count(action_type: str, *extra: str) -> int:
        sql = "SELECT COUNT(*) AS n FROM action_queue WHERE action_type = ?"
        for clause in extra:
            sql += f" AND {clause}"
        return int(conn.execute(sql, (action_type,)).fetchone()["n"] or 0)

    def interaction_count(action_type: str, *extra: str) -> int:
        sql = "SELECT COUNT(*) AS n FROM interactions WHERE action_type = ?"
        for clause in extra:
            sql += f" AND {clause}"
        return int(conn.execute(sql, (action_type,)).fetchone()["n"] or 0)

    def draft_count(*extra: str) -> int:
        sql = "SELECT COUNT(*) AS n FROM comment_drafts"
        if extra:
            sql += " WHERE " + " AND ".join(extra)
        return int(conn.execute(sql).fetchone()["n"] or 0)

    like = {
        "eligible": like_eligible,
        # 실행된 건수(Dry Run 포함) — Interaction 기록이 남은 것만 센다.
        "executed": interaction_count("LIKE"),
        "confirmed": interaction_count("LIKE", "success = 1", "dry_run = 0"),
        # 실행 실패/보류는 품질이 아니라 실행 건강 신호다(18C.1과 같은 분류).
        "blocked": queue_count("LIKE", "status IN ('FAILED', 'SKIPPED', 'CANCELLED')"),
    }
    comment = {
        "eligible": comment_eligible,
        "quality_pass": draft_count("quality_ok = 1"),
        "quality_reject": draft_count("quality_ok = 0"),
        "dry_run": interaction_count("COMMENT", "dry_run = 1"),
        # 19G: 게시 확인까지 끝난 것만 confirmed로 센다.
        "confirmed": interaction_count("COMMENT", "success = 1", "dry_run = 0"),
        # 실제로 올렸지만 확인되지 않은 건(= 보냈다고 단정하지 않는다).
        "unknown": interaction_count("COMMENT", "success = 0", "dry_run = 0"),
    }
    return {
        "like": like,
        "comment": comment,
        "thresholds": {
            "like": int(policy.like_threshold),
            "comment": int(policy.comment_threshold),
        },
    }
