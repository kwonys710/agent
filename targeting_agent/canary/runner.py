"""Limited Live Canary Runner (Phase 18E).

실제 Instagram LIKE를 **아주 적은 수만** 눌러 LIVE 경로를 검증한다.

안전 설계의 핵심 두 가지:

1. **config.yaml은 건드리지 않는다.**
   LIVE 전환은 이 프로세스 메모리 안의 복사본에서만 일어난다. 프로세스가
   죽어도 파일은 DRY_RUN 그대로다 — crash 때문에 계정이 LIVE로 남는 일이 없다.

2. **한도는 파일에 남는다.**
   Scheduler가 잘못 떠도, Task 삭제에 실패해도, 상태 파일이 "다 썼다"고 하면
   Runner는 아무 것도 하지 않는다. Scheduler는 안전장치가 아니다.

COMMENT는 이 경로로 들어올 수 없다. Queue에서 LIKE만 골라 승인하고,
Executor가 COMMENT 호출 자체를 위반으로 막는다(이중).
"""
from __future__ import annotations

import copy
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..actions.controller import ActionController
from ..actions.rate_limiter import RateLimiter
from ..core.config import Config
from ..core.database import (
    approve_action,
    get_connection,
    init_db,
    utc_now,
)
from ..core.exceptions import PlatformWarningError
from ..core.logger import get_logger
from ..core.models import ActionType
from .executor import CanaryViolation, LikeOnlyExecutor, build_like_only_executor
from .state import (
    ARMED,
    COMPLETED,
    FAILED,
    RUNNING,
    STOPPED_ACTION_BLOCK,
    STOPPED_CHALLENGE,
    STOPPED_LOGIN,
    STOPPED_UNKNOWN_WRITE,
    STOPPED_VIOLATION,
    STOPPED_WARNING,
    CanaryState,
    arm,
    load_state,
    save_state,
    state_path,
)

logger = get_logger("canary.runner")

APPROVED_BY_CANARY = "live_canary"

# Run이 끝난 이유.
OUTCOME_EXECUTED = "EXECUTED"
OUTCOME_NO_BUDGET = "NO_BUDGET"
OUTCOME_TOO_SOON = "TOO_SOON"
OUTCOME_EXPIRED = "WINDOW_EXPIRED"
OUTCOME_TERMINAL = "ALREADY_FINISHED"
OUTCOME_NO_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"
OUTCOME_SESSION = "SESSION_NOT_READY"
OUTCOME_STOPPED = "STOPPED"

# 중단 사유 문자열 → Canary 상태. **구체적인 것부터** 본다.
# Controller가 'platform_warning:<실제 사유>' 로 감싸기 때문에, 일반적인
# PLATFORM_WARNING을 먼저 맞히면 LOGIN_REQUIRED 같은 진짜 사유가 묻힌다.
_STOP_STATUS: tuple[tuple[str, str], ...] = (
    ("CRITICAL_CANARY_VIOLATION", STOPPED_VIOLATION),
    ("ACTION_BLOCKED", STOPPED_ACTION_BLOCK),
    ("CHALLENGE", STOPPED_CHALLENGE),
    ("LOGIN_REQUIRED", STOPPED_LOGIN),
    ("PLATFORM_WARNING", STOPPED_WARNING),
)

# canonical_url은 **그 게시물을 실제로 열어** link[rel=canonical]을 읽었을 때만
# 채워진다. 그래서 이 조건은 "우리가 존재를 확인한 게시물만 누른다"는 뜻이다.
# CSV로 들여온 샘플처럼 한 번도 열어 본 적 없는 주소에는 실제 동작을 하지 않는다.
_ELIGIBLE_SQL = """
SELECT q.action_id, q.media_pk, q.creator_id, q.target_score, c.username
FROM action_queue q
LEFT JOIN creators c ON c.creator_id = q.creator_id
LEFT JOIN candidate_media m ON m.media_pk = q.media_pk
WHERE q.status = 'PENDING'
  AND q.action_type = 'LIKE'
  AND q.approved_at IS NULL
  AND q.target_score >= ?
  AND TRIM(COALESCE(m.canonical_url, '')) <> ''
  AND COALESCE(c.username, '') NOT LIKE 'unresolved:%'
  AND NOT EXISTS (
        SELECT 1 FROM interactions i
         WHERE i.media_pk = q.media_pk AND i.action_type = 'LIKE' AND i.dry_run = 0
  )
ORDER BY q.target_score DESC, q.action_id
LIMIT ?
"""


@dataclass
class CanaryRunResult:
    """Run 1회 결과."""

    canary_id: str = ""
    outcome: str = OUTCOME_NO_BUDGET
    live: bool = False
    budget: int = 0
    approved: int = 0
    attempted: int = 0
    confirmed: int = 0
    unknown_write: int = 0
    failed: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    stop_reason: str = ""
    state_status: str = ""
    total_live_likes: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome in (OUTCOME_SESSION, OUTCOME_STOPPED) else 0

    def as_rows(self) -> dict[str, str]:
        return {
            "Canary ID": self.canary_id or "-",
            "결과": self.outcome,
            "LIVE 여부": "LIVE" if self.live else "DRY_RUN",
            "이번 Run 예산": str(self.budget),
            "승인(live_canary)": str(self.approved),
            "시도 / 확인": f"{self.attempted} / {self.confirmed}",
            "확인 불가(UNKNOWN)": str(self.unknown_write),
            "실패 / 보류": f"{self.failed} / {self.skipped}",
            "보류 사유": ", ".join(f"{k}={v}" for k, v in sorted(self.skip_reasons.items())) or "없음",
            "누적 실제 LIKE": str(self.total_live_likes),
            "Canary 상태": self.state_status,
            "중단 사유": self.stop_reason or "-",
        }


def live_override(config: Config) -> Config:
    """**이 프로세스 안에서만** LIVE인 설정 사본을 만든다.

    원본 config는 손대지 않는다. 사본이므로 프로세스가 끝나면 함께 사라지고,
    crash가 나도 config.yaml은 DRY_RUN 그대로 남는다.
    """
    raw = copy.deepcopy(config.raw)
    raw["browser_executor"] = {**raw.get("browser_executor", {}), "mode": "LIVE"}
    actions = {**raw.get("actions", {})}
    actions["execution"] = {**actions.get("execution", {}), "dry_run": False}
    # Canary는 댓글을 만들지도 실행하지도 않는다.
    actions["enable_comment"] = False
    raw["actions"] = actions
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def _eligible_actions(
    conn: sqlite3.Connection, *, like_threshold: float, limit: int
) -> list[sqlite3.Row]:
    """임계값을 넘고 아직 실제로 누르지 않은 LIKE Action만 고른다.

    임계값은 운영 설정 그대로다 — Canary를 채우려고 낮추지 않는다.
    """
    return list(conn.execute(_ELIGIBLE_SQL, (float(like_threshold), int(limit))).fetchall())


def _count_live_likes(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE dry_run = 0 AND success = 1 AND action_type = 'LIKE'"
    ).fetchone()
    return int(row[0]) if row else 0


def run_canary(
    config: Config,
    *,
    conn: Optional[sqlite3.Connection] = None,
    live: bool = True,
    now: Optional[datetime] = None,
    executor: Optional[LikeOnlyExecutor] = None,
    state: Optional[CanaryState] = None,
) -> CanaryRunResult:
    """Canary 1회. 예외를 밖으로 내보내지 않고 결과로 돌려준다.

    `live=False`면 전 구간을 DRY_RUN으로만 돈다(리허설).
    """
    moment = now or datetime.now(timezone.utc)
    path = state_path(config.data_dir)
    owns_state = state is None
    current = state if state is not None else load_state(path)
    result = CanaryRunResult(live=live)

    owns_connection = conn is None
    if conn is None:
        conn = get_connection(config.db_path)
        init_db(conn)

    try:
        # --- 끝난 Canary는 무엇을 해도 아무 것도 하지 않는다 ----------------
        if current.terminal:
            result.outcome = OUTCOME_TERMINAL
            result.messages.append(f"Canary가 이미 종료되었습니다({current.status}).")
            return _finish(result, current, path, save=owns_state, run_done=False)

        if current.status == "NOT_STARTED":
            if not live:
                # 리허설은 아무 것도 쓰지 않으므로 Canary 창을 열지 않는다.
                # 시계를 먼저 돌려 두면 정작 쓸 수 있을 때 기간이 줄어든다.
                owns_state = False
            arm(current, now=moment)
        current.roll_day(now=moment)
        result.canary_id = current.canary_id

        if current.is_expired(now=moment):
            current.status = COMPLETED
            current.stop_reason = "window_expired"
            current.completed_at = moment.isoformat(timespec="seconds")
            result.outcome = OUTCOME_EXPIRED
            return _finish(result, current, path, save=owns_state, run_done=False)

        if live and not current.interval_ok(now=moment):
            allowed = current.next_allowed_run()
            result.outcome = OUTCOME_TOO_SOON
            result.messages.append(
                f"다음 Run 허용 시각까지 기다립니다: {allowed.isoformat(timespec='seconds') if allowed else '-'}"
            )
            return _finish(result, current, path, save=owns_state, run_done=False)

        budget = current.budget_for_run() if live else min(current.max_per_run, 2)
        result.budget = budget
        if budget <= 0:
            result.outcome = OUTCOME_NO_BUDGET
            return _finish(result, current, path, save=owns_state, run_done=False)

        # --- 설정: LIVE는 메모리 사본에서만 ---------------------------------
        runtime = live_override(config) if live else config
        runtime.raw["actions"] = {
            **runtime.raw.get("actions", {}),
            "execution": {
                **runtime.raw.get("actions", {}).get("execution", {}),
                "max_actions_per_run": budget,
            },
        }
        result.live = bool(live and not runtime.dry_run)

        # --- 후보 선정 + 승인 ------------------------------------------------
        like_threshold = float(config.get("actions.require_score_for_like", 75))
        rows = _eligible_actions(conn, like_threshold=like_threshold, limit=budget)
        if not rows:
            result.outcome = OUTCOME_NO_CANDIDATES
            result.messages.append(
                f"LIKE 임계값({like_threshold:.0f}) 이상인 미실행 후보가 없습니다 — "
                "임계값을 낮추지 않고 이번 Run을 정상 종료합니다."
            )
            return _finish(result, current, path, save=owns_state, run_done=live)

        for row in rows:
            if approve_action(conn, int(row["action_id"]), by=APPROVED_BY_CANARY):
                result.approved += 1
        conn.commit()

        # --- 실행 ------------------------------------------------------------
        current.status = RUNNING
        if owns_state:
            save_state(path, current)

        like_executor = executor or build_like_only_executor(
            runtime, conn, run_id=current.canary_id
        )
        controller = ActionController(
            runtime, conn, like_executor, RateLimiter(runtime, conn)
        )
        before = _count_live_likes(conn)
        try:
            summary = controller.run()
        except CanaryViolation as exc:
            current.stop(STOPPED_VIOLATION, str(exc), now=moment)
            result.outcome = OUTCOME_STOPPED
            result.stop_reason = str(exc)
            conn.commit()
            return _finish(result, current, path, save=owns_state, run_done=True)
        except PlatformWarningError as exc:
            status = _stop_status_for(str(exc))
            current.stop(status, str(exc), now=moment)
            result.outcome = OUTCOME_STOPPED
            result.stop_reason = str(exc)
            conn.commit()
            return _finish(result, current, path, save=owns_state, run_done=True)

        result.attempted = like_executor.attempted
        result.confirmed = like_executor.confirmed
        result.unknown_write = like_executor.unknown_write
        result.failed = summary.failed
        result.skipped = summary.skipped
        result.skip_reasons = dict(summary.skip_reasons)
        result.outcome = OUTCOME_EXECUTED

        # 상태에는 **DB가 실제로 기록한 건수만** 반영한다(세기를 두 곳에서 하지 않는다).
        newly_written = max(0, _count_live_likes(conn) - before)
        for row in rows[:newly_written]:
            current.record_like(int(row["media_pk"]), now=moment)

        if summary.halted:
            current.stop(
                _stop_status_for(summary.halt_reason or ""), summary.halt_reason or "halted",
                now=moment,
            )
            result.outcome = OUTCOME_STOPPED
            result.stop_reason = summary.halt_reason or ""
        elif like_executor.unknown_write:
            # 눌렸는지 모르는 건이 있으면 이후 LIVE 쓰기를 멈춘다(§29).
            current.stop(STOPPED_UNKNOWN_WRITE, "unknown_write_state", now=moment)
            result.outcome = OUTCOME_STOPPED
            result.stop_reason = "unknown_write_state"

        conn.commit()
        return _finish(result, current, path, save=owns_state, run_done=True)

    except Exception as exc:  # noqa: BLE001 - Run 하나의 실패가 상태를 망가뜨리지 않게 한다
        logger.exception("Canary Run 실패")
        current.stop(FAILED, f"{type(exc).__name__}: {exc}", now=moment)
        result.outcome = OUTCOME_STOPPED
        result.stop_reason = str(exc)
        return _finish(result, current, path, save=owns_state, run_done=False)
    finally:
        if owns_connection:
            conn.close()


def _stop_status_for(reason: str) -> str:
    upper = (reason or "").upper()
    for marker, status in _STOP_STATUS:
        if marker in upper:
            return status
    return STOPPED_WARNING


def _finish(
    result: CanaryRunResult,
    state: CanaryState,
    path,
    *,
    save: bool,
    run_done: bool,
) -> CanaryRunResult:
    if run_done and not state.terminal:
        state.finish_run()
    elif run_done:
        state.runs += 1
        state.last_run_at = utc_now()
    if state.status == RUNNING:
        state.status = ARMED
    result.state_status = state.status
    result.total_live_likes = state.total_live_likes
    result.stop_reason = result.stop_reason or state.stop_reason
    if save:
        save_state(path, state)
    return result


def format_result(result: CanaryRunResult) -> str:
    lines = [
        "=" * 56,
        " DailyReels Targeting Agent — Limited Live Canary",
        "=" * 56,
    ]
    for key, value in result.as_rows().items():
        lines.append(f" {key:22} : {value}")
    for message in result.messages:
        lines.append(f"   - {message}")
    lines.append("=" * 56)
    return "\n".join(lines)
