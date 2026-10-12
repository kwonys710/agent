"""COMMENT Live Canary (Phase 19M).

실제 공개 댓글을 **아주 적게, 확인하면서** 달아 보는 경로다.
LIKE Canary(`canary/runner.py`)와 같은 골격을 쓰되, 다음을 더 조인다.

- 실행당 기본 **1건**. 상한은 Executor와 Controller 양쪽에서 막는다.
- Activity 상태가 NORMAL일 때만 시작한다(CAUTION/STOPPED면 아예 하지 않는다).
- 점수 임계값과 댓글 품질 Gate를 **낮추지 않는다**. 후보가 없으면 그냥 끝낸다.
- 게시 결과를 확인한다. 확인 불가(UNKNOWN)면 Run 전체 중단 + 재시도 금지.
- LIVE는 **메모리 사본에서만** 켜진다. config.yaml은 DRY_RUN 그대로 남는다.
- 실행 중 로그인 필요/Challenge/경고/작업 차단이 보이면 즉시 멈춘다.

탐지 회피·우회는 하지 않는다. 사람 행동 위장도 하지 않는다.
"""
from __future__ import annotations

import copy
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..actions.activity import ActivityState
from ..actions.activity import status as activity_status
from ..actions.controller import ActionController
from ..actions.policy import ActionPolicy, comment_eligibility
from ..actions.rate_limiter import RateLimiter
from ..core.config import Config
from ..core.database import (
    APPROVED_BY_OPERATOR,
    approve_action,
    get_connection,
    init_db,
    utc_now,
)
from ..core.logger import get_logger
from ..scheduler.lock import SchedulerLock
from .comment_executor import CommentOnlyExecutor, build_comment_only_executor
from .resume import is_real_instagram_target

logger = get_logger("canary.comment_runner")

APPROVED_BY_COMMENT_CANARY = "comment_canary"

OUTCOME_DONE = "DONE"
OUTCOME_NO_CANDIDATES = "NO_CANDIDATES"
OUTCOME_BLOCKED_ACTIVITY = "BLOCKED_ACTIVITY"
OUTCOME_LOCKED = "LOCKED"
OUTCOME_DISABLED = "DISABLED"
OUTCOME_STOPPED = "STOPPED"
OUTCOME_FAILED = "FAILED"

# 승인 대기(approved_at IS NULL)인 COMMENT 중, 아직 실제로 달지 않은 것만 고른다.
_ELIGIBLE_SQL = """
SELECT q.action_id, q.media_pk, q.creator_id, q.target_score, q.comment_text,
       c.username, m.media_id, m.canonical_url, m.caption
FROM action_queue q
LEFT JOIN creators c ON c.creator_id = q.creator_id
LEFT JOIN candidate_media m ON m.media_pk = q.media_pk
WHERE q.status = 'PENDING'
  AND q.action_type = 'COMMENT'
  AND q.target_score >= ?
  AND TRIM(COALESCE(m.canonical_url, '')) <> ''
  AND TRIM(COALESCE(m.caption, '')) <> ''
  AND COALESCE(c.username, '') NOT LIKE 'unresolved:%'
  AND NOT EXISTS (
        SELECT 1 FROM interactions i
         WHERE i.media_pk = q.media_pk AND i.action_type = 'COMMENT' AND i.dry_run = 0
  )
ORDER BY q.target_score DESC, q.action_id ASC
LIMIT ?
"""


@dataclass
class CommentCanaryResult:
    """COMMENT Canary 1회 결과."""

    outcome: str = OUTCOME_DONE
    live: bool = False
    budget: int = 0
    candidates: int = 0
    approved: int = 0
    confirmed: int = 0
    unknown: int = 0
    not_sent: int = 0
    skipped: int = 0
    like_skipped: int = 0
    activity_state: str = ActivityState.NORMAL.value
    stop_reason: str = ""
    posted: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    messages: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.outcome in (OUTCOME_DONE, OUTCOME_NO_CANDIDATES, OUTCOME_LOCKED, OUTCOME_DISABLED):
            return 0
        return 1

    def as_rows(self) -> dict[str, str]:
        return {
            "결과": self.outcome,
            "실제 수행": "예" if self.live else "아니오(DRY_RUN)",
            "이번 상한": str(self.budget),
            "자격 후보": str(self.candidates),
            "승인": str(self.approved),
            "게시 확인": str(self.confirmed),
            "확인 불가(UNKNOWN)": str(self.unknown),
            "미게시": str(self.not_sent),
            "보류": str(self.skipped),
            "활동 상태": self.activity_state,
            "중단 사유": self.stop_reason or "-",
        }


def comment_live_override(config: Config, *, budget: int) -> Config:
    """**이 프로세스 안에서만** LIVE인 설정 사본.

    원본 config는 손대지 않는다 — crash가 나도 config.yaml은 DRY_RUN 그대로다.
    LIKE Canary의 live_override와 대칭이며, 여기서는 COMMENT만 켠다.
    """
    raw = copy.deepcopy(config.raw)
    raw["browser_executor"] = {**raw.get("browser_executor", {}), "mode": "LIVE"}
    actions = {**raw.get("actions", {})}
    actions["execution"] = {
        **actions.get("execution", {}),
        "dry_run": False,
        "max_actions_per_run": budget,
        # 한 건이라도 실패/중단이면 그 자리에서 멈춘다.
        "stop_if_error_count": 1,
    }
    actions["enable_comment"] = True
    # 이 Run은 COMMENT만 한다. LIKE는 Executor가 건너뛴다.
    actions["enable_like"] = False
    raw["actions"] = actions
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def eligible_comments(
    conn: sqlite3.Connection, config: Config, *, limit: int
) -> list[sqlite3.Row]:
    """임계값·품질 Gate를 모두 통과한 실제 게시물만 고른다(기준을 낮추지 않는다)."""
    threshold = ActionPolicy.from_config(config).comment_threshold
    rows = conn.execute(_ELIGIBLE_SQL, (float(threshold), int(limit) * 5)).fetchall()
    chosen: list[sqlite3.Row] = []
    seen_creators: set[int] = set()
    for row in rows:
        if not is_real_instagram_target(
            str(row["canonical_url"] or ""), str(row["media_id"] or "")
        ):
            continue  # 합성 데이터(SAMPLE001 등)에 실제 댓글을 달지 않는다
        if int(row["creator_id"]) in seen_creators:
            continue
        if not comment_eligibility(conn, config, int(row["media_pk"]), row["target_score"]).ok:
            continue
        seen_creators.add(int(row["creator_id"]))
        chosen.append(row)
        if len(chosen) >= limit:
            break
    return chosen


def run_comment_canary(
    config: Config,
    *,
    conn: Optional[sqlite3.Connection] = None,
    live: bool = True,
    now: Optional[datetime] = None,
    page: Optional[Any] = None,
    executor: Optional[CommentOnlyExecutor] = None,
) -> CommentCanaryResult:
    """COMMENT Canary 1회. 예외를 밖으로 내보내지 않고 결과로 돌려준다.

    `live=False`면 전 구간을 DRY_RUN으로 점검한다(실제 게시 0건).
    """
    moment = now or datetime.now(timezone.utc)
    result = CommentCanaryResult(started_at=moment.isoformat(timespec="seconds"))

    if live and not bool(config.get("canary.comment.enabled", False)):
        result.outcome = OUTCOME_DISABLED
        result.messages.append(
            "canary.comment.enabled=false — 실제 댓글은 운영자가 명시적으로 켤 때만 수행합니다."
        )
        result.finished_at = utc_now()
        return result

    lock = SchedulerLock(
        path=config._resolve_path(
            config.get("canary.comment.lock.path", "data/.canary_comment.lock")
        ),
        stale_minutes=int(config.get("canary.comment.lock.stale_minutes", 30)),
        enabled=bool(config.get("canary.comment.lock.enabled", True)),
    )
    if not lock.acquire(moment):
        result.outcome = OUTCOME_LOCKED
        result.messages.append("다른 Canary가 진행 중이어서 종료합니다.")
        result.finished_at = utc_now()
        return result

    owns_connection = conn is None
    comment_executor: Optional[CommentOnlyExecutor] = executor
    try:
        if conn is None:
            conn = get_connection(config.db_path)
            init_db(conn)

        # --- 계정 건강 상태: NORMAL이 아니면 시작하지 않는다 -----------------
        activity = activity_status(conn, config, now=moment)
        result.activity_state = activity.state.value
        if live and activity.state is not ActivityState.NORMAL:
            result.outcome = OUTCOME_BLOCKED_ACTIVITY
            result.stop_reason = f"activity_{activity.state.value}"
            result.messages.append(
                f"활동 상태가 {activity.state.value}({activity.reason or '-'})이므로 "
                "실제 댓글을 달지 않습니다."
            )
            return result

        budget = max(0, int(config.get("canary.comment.max_comments_per_run", 1)))
        result.budget = budget
        if budget <= 0:
            result.outcome = OUTCOME_NO_CANDIDATES
            result.messages.append("이번 Run 상한이 0입니다.")
            return result

        runtime = comment_live_override(config, budget=budget) if live else config
        result.live = bool(live and not runtime.dry_run)

        rows = eligible_comments(conn, config, limit=budget)
        result.candidates = len(rows)
        if not rows:
            result.outcome = OUTCOME_NO_CANDIDATES
            result.messages.append(
                "COMMENT 임계값과 품질 Gate를 통과한 미실행 후보가 없습니다 — "
                "기준을 낮추지 않고 이번 Run을 정상 종료합니다."
            )
            return result

        for row in rows:
            if approve_action(conn, int(row["action_id"]), by=APPROVED_BY_COMMENT_CANARY):
                result.approved += 1
        conn.commit()

        comment_executor = executor or build_comment_only_executor(
            runtime, conn, run_id=f"comment-canary-{moment.strftime('%Y%m%d-%H%M%S')}",
            page=page, max_comments=budget,
        )
        controller = ActionController(runtime, conn, comment_executor, RateLimiter(runtime, conn))
        summary = controller.run()

        result.confirmed = comment_executor.confirmed
        result.unknown = comment_executor.unknown
        result.not_sent = comment_executor.not_sent
        result.like_skipped = comment_executor.like_skipped
        result.skipped = summary.skipped
        result.posted = list(comment_executor.posted)

        if summary.halted or comment_executor.halt_reason:
            result.outcome = OUTCOME_STOPPED
            result.stop_reason = comment_executor.halt_reason or summary.halt_reason
            result.messages.append(
                "확인되지 않은 상태로 중단했습니다 — 같은 댓글을 다시 보내지 않습니다."
                if comment_executor.halt_reason
                else f"실행이 중단되었습니다: {summary.halt_reason}"
            )
            # 플랫폼 신호면 활동 상태를 악화 방향으로 반영한다.
            _observe_signal(conn, result.stop_reason)
        elif summary.failed:
            result.outcome = OUTCOME_FAILED
            result.messages.append("게시에 실패했습니다(재시도하지 않습니다).")
    except Exception as exc:  # noqa: BLE001 - 실행 자체의 실패만 결과로 남긴다
        logger.exception("COMMENT Canary 실패")
        result.outcome = OUTCOME_FAILED
        result.messages.append(str(exc)[:300])
    finally:
        result.finished_at = utc_now()
        lock.release()
        if owns_connection and conn is not None:
            conn.close()

    logger.info(
        "COMMENT Canary 종료: %s (확인 %d / UNKNOWN %d / 미게시 %d, 실제 %s)",
        result.outcome,
        result.confirmed,
        result.unknown,
        result.not_sent,
        result.live,
    )
    return result


def _observe_signal(conn: sqlite3.Connection, reason: str) -> None:
    from ..actions.activity import observe

    token = str(reason or "").split(":", 1)[0].strip().upper()
    if token and token != "UNKNOWN_COMMENT_STATE":
        observe(conn, token)


def format_result(result: CommentCanaryResult) -> str:
    """CLI 출력용 요약."""
    lines = [
        "",
        "=" * 56,
        " DailyReels Targeting Agent — COMMENT Live Canary",
        "=" * 56,
    ]
    lines.extend(f" {key:<18}: {value}" for key, value in result.as_rows().items())
    if result.posted:
        lines.append(" 게시한 댓글      :")
        lines.extend(f"   - {text}" for text in result.posted)
    for message in result.messages:
        lines.append(f" 메모             : {message}")
    lines.append("=" * 56)
    return "\n".join(lines)
