"""Eligibility Acquisition & Auto-Resume (Phase 18E.1).

Canary가 멈춰 있던 이유는 하나였다 — **누를 만한 실제 후보가 없었다.**
사람이 매번 깨워 주지 않아도 스스로 그 상태를 벗어나게 한다.

    AI 한도 회복 → 제한된 Discovery → 분석 → 자격 확인
    → 리허설 → 실제 LIKE → 다음 Run 예약

기다리는 것은 실패가 아니다. 다음 둘은 정상 상태로 저장하고 조용히 끝낸다.

    WAITING_AI_BUDGET  — Claude 일일 한도를 다 썼다
    WAITING_ELIGIBLE   — 임계값을 넘는 실제 후보가 아직 없다

둘 중 어느 쪽에서도 임계값을 낮추거나 샘플 데이터를 대상으로 올리지 않는다.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.config import Config
from ..core.database import get_connection, init_db, today_str
from ..core.logger import get_logger
from ..discovery.browser_runner import (
    STATUS_DISABLED,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_LOCKED,
    STATUS_PARTIAL,
    STATUS_SESSION_STOPPED,
    STATUS_SUCCESS,
)
from .runner import (
    OUTCOME_EXECUTED,
    OUTCOME_NO_CANDIDATES,
    CanaryRunResult,
    _eligible_actions,
    _stop_status_for,
    run_canary,
)
from .state import (
    READY_FOR_LIVE,
    STOPPED_SAFETY,
    WAITING_AI_BUDGET,
    WAITING_ELIGIBLE,
    WAITING_NEXT_RUN,
    CanaryState,
    arm,
    load_state,
    save_state,
    state_path,
)

logger = get_logger("canary.resume")

# 한 번 깨어날 때 새로 찾아볼 후보 수 상한(§6). 과하게 훑지 않는다.
MAX_DISCOVERY_PER_RESUME = 20

STEP_BLOCKED = "BLOCKED"
STEP_WAITING_BUDGET = "WAITING_AI_BUDGET"
STEP_WAITING_ELIGIBLE = "WAITING_ELIGIBLE"
STEP_DISCOVERED = "DISCOVERED"
STEP_REHEARSED = "REHEARSED"
STEP_LIVE = "LIVE"
STEP_NOOP = "NOOP"

# 실제 Instagram 게시물 주소만 대상으로 삼는다(§11).
_REAL_URL_RE = re.compile(
    r"^https://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[0-9A-Za-z_-]+/?$"
)
# 테스트·예시 데이터에서 쓰는 식별자. 실제 동작 대상이 될 수 없다.
_SYNTHETIC_MARKERS = ("sample", "fixture", "test", "example", "dummy", "localhost", "127.0.0.1")


def is_real_instagram_target(url: str, media_id: str = "") -> bool:
    """실제로 눌러도 되는 Instagram 주소인지.

    모양만 맞는 합성 데이터(SAMPLE001 등)를 걸러 낸다 — 존재하지 않는 주소에
    실제 동작을 하면 없는 페이지를 훑는 꼴이 된다.
    """
    text = (url or "").strip()
    if not _REAL_URL_RE.match(text):
        return False
    lowered = f"{text} {media_id}".lower()
    return not any(marker in lowered for marker in _SYNTHETIC_MARKERS)


@dataclass
class DiscoveryOutcome:
    """Discovery 1회의 결과 중 Resume이 판단에 쓰는 것만.

    `status`는 Discovery의 값 **그대로**다(STATUS_*). Canary 상태로 복사하지 않는다 —
    Discovery의 FAILED와 Canary의 FAILED(영구 종료)는 이름만 같다.
    """

    status: str = STATUS_FAILED
    stop_reason: str = ""
    message: str = ""
    added: int = 0
    analyzed: int = 0


# browser_runner 의 STATUS_* 와 **중복**이다. 새 status 가 추가되면 여기와 아래 분기를 더해야 하며,
# 그때까지는 "알 수 없는 Discovery 상태" 경고가 뜨고 FAILED 로 처리된다(경고가 drift 신호다).
_KNOWN_STATUSES = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_EMPTY,
        STATUS_PARTIAL,
        STATUS_FAILED,
        STATUS_SESSION_STOPPED,
        STATUS_LOCKED,
        STATUS_DISABLED,
    }
)

# SESSION_STOPPED 의 stop_reason 별 설명. Discovery 원문 안내("다시 시도하세요")는
# Discovery 층에서만 맞다 — Canary 는 종료되므로 그대로 전달하지 않는다.
_SESSION_CAUSE = {
    "LOGIN_REQUIRED": "Instagram 로그인이 필요한 상태가 감지되었다",
    "CHALLENGE": "본인 확인(Challenge)이 감지되었다",
    "PLATFORM_WARNING": "이용 제한 경고가 감지되었다",
}
# UNKNOWN 도 의도적으로 TERMINAL 이다 — 판독하지 못한 상태에서 브라우저를 다시 여는 것은
# 확인되지 않은 전제(실제로는 Challenge 였을 수 있다) 위에서 활동을 재개하는 일이다.
_UNKNOWN_CAUSE = "세션 상태를 판독하지 못했다(플랫폼 신호가 확정된 것은 아니다)"


def _recovery_notice(status: str, path) -> str:
    """종료 상태에서 실제로 가능한 복구 절차. 재개용 CLI 는 일부러 없다(운영자 결정)."""
    return (
        f"Canary가 종료 상태({status})로 들어갔다. 재로그인·재실행만으로는 재개되지 않는다"
        "(다음 Resume은 NOOP). 재개 명령(CLI)은 없다 — 플랫폼 상태를 직접 확인해 해소됐을 때에만 "
        f"운영자가 상태 파일 {path} 의 \"status\"를 \"WAITING_NEXT_RUN\"으로 바꾸고 "
        "\"stop_reason\"·\"completed_at\"을 빈 문자열(\"\")로 비운다."
    )


# Discovery가 실제로 검색을 돌려 본 결과(후보가 없을 수는 있다). 이때만 "후보 없음"이 의미를 갖는다.
_RAN_STATUSES = frozenset({STATUS_SUCCESS, STATUS_PARTIAL, STATUS_EMPTY})


@dataclass
class ResumeResult:
    """깨어나서 어디까지 갔는지."""

    step: str = STEP_NOOP
    status: str = ""
    reason: str = ""
    ai_used: int = 0
    ai_limit: int = 0
    eligible: int = 0
    highest_score: Optional[float] = None
    discovered: int = 0
    analyzed: int = 0
    rehearsal_ok: Optional[bool] = None
    live: Optional[CanaryRunResult] = None
    messages: list[str] = field(default_factory=list)
    # Discovery가 돌려준 원래 상태. Canary 상태(status)와 섞이지 않게 따로 둔다.
    source_status: str = ""
    source_stop_reason: str = ""

    @property
    def ai_remaining(self) -> int:
        return max(0, self.ai_limit - self.ai_used)

    @property
    def exit_code(self) -> int:
        return 1 if self.step == STEP_BLOCKED else 0

    def _source_text(self) -> str:
        if not self.source_status:
            return "-"
        if self.source_stop_reason:
            return f"{self.source_status} ({self.source_stop_reason})"
        return self.source_status

    def as_rows(self) -> dict[str, str]:
        rows = {
            "단계": self.step,
            "Canary 상태": self.status or "-",
            "사유": self.reason or "-",
            "Claude 한도": f"{self.ai_used} / {self.ai_limit} (남음 {self.ai_remaining})",
            "자격 후보(>=임계값)": str(self.eligible),
            "실제 후보 최고점(DB 누적)": (
                f"{self.highest_score:.2f}" if self.highest_score is not None else "-"
            ),
            "이번에 새로 찾은 후보": str(self.discovered),
            "Discovery 상태": self._source_text(),
            "리허설": {None: "미수행", True: "통과", False: "실패"}[self.rehearsal_ok],
        }
        if self.live is not None:
            rows["실제 LIKE(시도/확인)"] = f"{self.live.attempted} / {self.live.confirmed}"
        return rows


def _ai_usage(
    conn: sqlite3.Connection, config: Config, *, now: Optional[datetime] = None
) -> tuple[int, int]:
    """오늘 쓴 Claude 호출 수와 한도. **기존 집계 방식을 그대로 쓴다**(§4).

    reset 시각을 따로 추측하지 않는다 — 기존 코드가 쓰는 날짜 경계를 그대로 읽는다.
    `now`를 주면 그 시점의 날짜로 본다(주지 않으면 실제 현재 시각).
    호출자가 넘긴 시각을 무시하면 날짜가 바뀔 때마다 결과가 달라진다.
    """
    from datetime import timedelta

    from ..ai.intelligence import DAILY_METRIC

    from ..core.database import get_stats

    offset = int(config.get("actions.daily_limits.timezone_offset_hours", 9))
    if now is None:
        date = today_str(offset)
    else:
        date = (now + timedelta(hours=offset)).strftime("%Y-%m-%d")
    used = int(get_stats(conn, date).get(DAILY_METRIC, 0))
    limit = int(config.get("ai.daily_request_limit", 20))
    return used, limit


def _eligible_summary(conn: sqlite3.Connection, config: Config) -> tuple[int, Optional[float]]:
    """자격 후보 수와 실제 후보 최고점(DB 전체 집계 — 이번 실행 결과가 아니다)."""
    threshold = float(config.get("actions.require_score_for_like", 75))
    # _eligible_actions가 이미 합성 주소를 걸러 준다(한 곳에서만 판단한다).
    eligible = len(_eligible_actions(conn, like_threshold=threshold, limit=50))
    best = conn.execute(
        "SELECT MAX(target_score) FROM candidate_media "
        "WHERE source = 'instagram_browser_search' AND target_score IS NOT NULL"
    ).fetchone()
    return eligible, (float(best[0]) if best and best[0] is not None else None)


def _run_discovery(
    config: Config, conn: sqlite3.Connection, *, budget: int
) -> DiscoveryOutcome:
    """제한된 Discovery + 분석을 1회 수행한다(기존 구현 재사용).

    새 Discovery 구조를 만들지 않는다. 한도는 남은 Claude 예산을 넘지 않는다.
    (added, analyzed)로 줄이지 않는다 — 상태를 버리면 LOCKED·차단·실패가 모두
    "후보 없음"으로 읽힌다.
    """
    # 함수 안 import 를 top-level 로 옮기지 마라 — 테스트가 browser_runner 모듈 속성을
    # 패치해 Discovery 를 대체한다(top-level import 는 패치를 빗나간다).
    from ..discovery.browser_runner import run_browser_discovery

    limits = {
        **config.raw.get("browser_discovery", {}).get("limits", {}),
        "max_queries_per_run": 2,
        "max_candidates_per_query": max(1, min(10, budget)),
        "max_candidates_per_run": max(1, min(MAX_DISCOVERY_PER_RESUME, budget)),
        "max_analyze_per_run": max(1, budget),
    }
    runtime = Config(
        raw={
            **config.raw,
            "browser_discovery": {
                **config.raw.get("browser_discovery", {}),
                "enabled": True,
                "limits": limits,
            },
            "ai": {**config.raw.get("ai", {}), "max_candidates_per_run": max(1, budget)},
        },
        path=config.path,
        base_dir=config.base_dir,
    )
    result = run_browser_discovery(runtime, conn, process=True)
    status = getattr(result, "status", None)
    message = str(getattr(result, "message", "") or "")
    if status not in _KNOWN_STATUSES:
        # 값이 없거나 모르는 상태를 성공으로 읽지 않는다(결과 객체의 기본값은 SUCCESS다).
        logger.warning("알 수 없는 Discovery 상태(%r) — FAILED로 취급합니다.", status)
        message = f"알 수 없는 Discovery 상태({status!r}) {message}".strip()
        status = STATUS_FAILED
    stop_reason = getattr(result, "stop_reason", None)
    return DiscoveryOutcome(
        status=status,
        # SessionState(str Enum)이든 문자열이든 값 문자열로 맞춘다.
        stop_reason=str(getattr(stop_reason, "value", stop_reason) or ""),
        message=message,
        added=int(getattr(result, "added", 0) or 0),
        analyzed=int(getattr(result, "analyzed", 0) or 0),
    )


def _stop_on_discovery_problem(
    state: CanaryState,
    result: ResumeResult,
    outcome: DiscoveryOutcome,
    moment: datetime,
    path,
) -> None:
    """Discovery가 후보 탐색 자체를 못 한 경우의 상태를 정한다.

    - SESSION_STOPPED: 플랫폼 이상 신호다. 기존 `_stop_status_for` 매핑으로 **종료**한다.
      종료 상태는 다음 Resume에서 조기 반환되므로 브라우저 Discovery를 다시 열지 않는다.
    - LOCKED / DISABLED / FAILED: 일시적이다. 종료하지 않고 다음 Run을 기다린다.
      (DISABLED 는 이 경로에서 도달 불가다 — `_run_discovery` 가 enabled=True 를 강제한다.
      방어적으로만 남긴 분기이며 커버리지를 근거로 쓰지 말 것.)
    """
    detail = f" — {outcome.message}" if outcome.message else ""
    budget_note = ""
    if result.ai_remaining <= 0:
        budget_note = f" (참고: Claude 일일 한도 {result.ai_used}/{result.ai_limit} 소진)"
    if outcome.status == STATUS_SESSION_STOPPED:
        cause = _SESSION_CAUSE.get(outcome.stop_reason, _UNKNOWN_CAUSE)
        reason = f"discovery_session_stopped:{outcome.stop_reason or 'UNKNOWN'} — {cause}"
        state.stop(_stop_status_for(outcome.stop_reason), reason, now=moment)
        result.step = STEP_BLOCKED
        result.status, result.reason = state.status, state.stop_reason
        # Discovery 원문 안내는 로그에만 남긴다(Canary 층에서는 "다시 시도" 가 거짓이다).
        logger.warning("Discovery 세션 중단 원문(%s):%s", outcome.stop_reason or "UNKNOWN", detail)
        result.messages.append(f"Discovery 세션 중단 — {cause}.")
        result.messages.append(_recovery_notice(state.status, path))
        return
    if outcome.status == STATUS_LOCKED:
        reason = f"Discovery lock 경쟁(다른 Discovery가 진행 중) — 이번엔 실행되지 않았다{detail}"
        result.step = STEP_NOOP
    elif outcome.status == STATUS_DISABLED:
        reason = f"browser_discovery가 비활성화되어 Discovery가 실행되지 않았다{detail}"
        result.step = STEP_NOOP
    else:  # STATUS_FAILED(그리고 아직 분기가 없는 새 status)
        reason = f"Discovery 실행 실패({outcome.status}){detail} — 후보 없음이 아니다"
        result.step = STEP_BLOCKED
    state.wait(WAITING_NEXT_RUN, reason + budget_note)
    result.status, result.reason = state.status, state.last_waiting_reason
    result.messages.append(f"Discovery 상태 {outcome.status}: 다음 Run에서 다시 시도한다.")


def resume_canary(
    config: Config,
    *,
    conn: Optional[sqlite3.Connection] = None,
    now: Optional[datetime] = None,
    allow_discovery: bool = True,
    allow_live: bool = True,
) -> ResumeResult:
    """Canary를 이어서 진행한다. 예외를 밖으로 내보내지 않는다."""
    moment = now or datetime.now(timezone.utc)
    result = ResumeResult()
    path = state_path(config.data_dir)
    state = load_state(path)

    # Scheduler가 겹쳐 떠도 두 개가 동시에 쓰지 않게 한다(기존 Lock 재사용).
    from ..scheduler.lock import SchedulerLock

    lock = SchedulerLock(
        path=config._resolve_path("data/.canary_resume.lock"),
        stale_minutes=int(config.get("autopilot.lock.stale_minutes", 60)),
        enabled=bool(config.get("autopilot.lock.enabled", True)),
        label="Canary Resume",
    )
    if not lock.acquire(moment):
        result.step = STEP_NOOP
        result.reason = "다른 Canary Resume이 진행 중입니다."
        return result

    owns_connection = conn is None

    try:
        # lock을 잡은 뒤의 모든 문장은 try 안에 둔다(예외가 나도 release가 돈다).
        if conn is None:
            conn = get_connection(config.db_path)
            init_db(conn)

        # --- 상태 파일이 사라졌는데 이미 실제로 누른 기록이 있으면 --------------
        # 새 Canary를 여는 것은 한도를 처음부터 다시 쓰겠다는 뜻이다. 막는다(§42).
        if not path.exists():
            already = int(
                conn.execute(
                    "SELECT COUNT(*) FROM interactions "
                    "WHERE dry_run = 0 AND success = 1 AND action_type = 'LIKE'"
                ).fetchone()[0]
            )
            if already:
                state.stop(
                    STOPPED_SAFETY,
                    f"state_missing_but_{already}_live_likes_recorded",
                    now=moment,
                )
                save_state(path, state)
                result.step = STEP_BLOCKED
                result.status = state.status
                result.reason = (
                    f"상태 파일이 없는데 실제 LIKE 기록이 {already}건 있습니다 — "
                    "한도를 처음부터 다시 쓰지 않도록 중단합니다."
                )
                return result

        # --- 끝난 Canary는 무엇을 해도 아무 것도 하지 않는다 -----------------
        if state.terminal:
            result.step = STEP_NOOP
            result.status = state.status
            result.reason = state.stop_reason or "이미 종료됨"
            if (state.stop_reason or "").startswith("discovery_session_stopped"):
                result.messages.append(_recovery_notice(state.status, path))
            return result

        if state.status == "NOT_STARTED":
            arm(state, now=moment)

        result.ai_used, result.ai_limit = _ai_usage(conn, config, now=moment)
        result.eligible, result.highest_score = _eligible_summary(conn, config)

        # --- 자격 후보가 없으면 찾아본다 -------------------------------------
        if result.eligible == 0 and allow_discovery and result.ai_remaining > 0:
            logger.info(
                "자격 후보가 없어 제한된 Discovery를 수행합니다(남은 Claude 예산 %d).",
                result.ai_remaining,
            )
            try:
                outcome = _run_discovery(config, conn, budget=result.ai_remaining)
            except Exception as exc:  # noqa: BLE001 - Discovery 실패가 상태를 망가뜨리지 않게
                logger.warning("Discovery 실패(%s) — 다음 기회에 다시 시도합니다.", type(exc).__name__)
                result.messages.append(f"Discovery 실패: {type(exc).__name__}")
                outcome = DiscoveryOutcome(status=STATUS_FAILED, message=type(exc).__name__)
            result.source_status = outcome.status
            result.source_stop_reason = outcome.stop_reason
            result.discovered, result.analyzed = outcome.added, outcome.analyzed
            if outcome.status not in (STATUS_LOCKED, STATUS_DISABLED):
                # Discovery가 실제로 시도된 때만 센다. (현재 이 값을 읽는 코드는 없다 —
                # 아무도 읽지 않는 카운터는 신호가 아니다. 판단 근거로 쓰지 말 것.)
                state.discovery_runs += 1
            result.ai_used, result.ai_limit = _ai_usage(conn, config, now=moment)
            result.eligible, result.highest_score = _eligible_summary(conn, config)
            result.step = STEP_DISCOVERED
            if outcome.status not in _RAN_STATUSES:
                if outcome.status == STATUS_SESSION_STOPPED or result.eligible == 0:
                    # 운영 장애를 "후보 없음"으로 바꾸지 않는다. 플랫폼 신호는 후보가
                    # 있어도 진행하지 않는다.
                    _stop_on_discovery_problem(state, result, outcome, moment, path)
                    save_state(path, state)
                    return result
                # LOCKED/FAILED 여도 동시 실행이 이미 자격 후보를 커밋했을 수 있다.
                # 상태는 남기되 기존 후보로 진행한다(불필요한 8시간 지연을 만들지 않는다).
                result.messages.append(
                    f"Discovery 상태 {outcome.status}"
                    f"{' — ' + outcome.message if outcome.message else ''}: "
                    f"자격 후보 {result.eligible}건이 이미 있어 기존 후보로 진행한다."
                )

        # --- 여전히 없으면 기다린다(실패가 아니다) ----------------------------
        if result.eligible == 0:
            threshold = float(config.get("actions.require_score_for_like", 75))
            best = f"{result.highest_score:.2f}" if result.highest_score is not None else "없음"
            if result.ai_remaining <= 0:
                state.wait(
                    WAITING_AI_BUDGET,
                    f"Claude 일일 한도 {result.ai_used}/{result.ai_limit} 소진 — "
                    "한도가 회복되면 다시 찾아본다.",
                )
                result.step = STEP_WAITING_BUDGET
            else:
                # best는 DB 전체 집계이지 이번 실행의 결과가 아니다.
                state.wait(
                    WAITING_ELIGIBLE,
                    f"임계값 {threshold:.0f} 이상인 실제 후보가 없다"
                    f"(DB 누적 최고점 {best} — 이번 실행 결과가 아님) — "
                    "임계값을 낮추지 않는다.",
                )
                result.step = STEP_WAITING_ELIGIBLE
            result.status, result.reason = state.status, state.last_waiting_reason
            save_state(path, state)
            return result

        # --- 리허설: 실제로 누르기 전에 전 구간을 DRY_RUN으로 확인 -------------
        if not state.rehearsal_passed_at:
            rehearsal = run_canary(config, conn=conn, live=False, now=moment, state=state)
            result.rehearsal_ok = rehearsal.outcome in (OUTCOME_EXECUTED, OUTCOME_NO_CANDIDATES)
            result.step = STEP_REHEARSED
            if not result.rehearsal_ok:
                state.stop(STOPPED_SAFETY, f"rehearsal_failed:{rehearsal.outcome}", now=moment)
                result.status, result.reason = state.status, state.stop_reason
                save_state(path, state)
                return result
            state.rehearsal_passed_at = moment.isoformat(timespec="seconds")
            state.status = READY_FOR_LIVE
            save_state(path, state)

        if not allow_live:
            result.status = state.status
            result.reason = "allow_live=False — 실제 쓰기는 하지 않았다."
            save_state(path, state)
            return result

        # --- 실제 LIKE -------------------------------------------------------
        live = run_canary(config, conn=conn, live=True, now=moment, state=state)
        result.live = live
        result.step = STEP_LIVE
        result.status = state.status
        result.reason = live.stop_reason or live.outcome
        save_state(path, state)
        return result

    except Exception as exc:  # noqa: BLE001
        logger.exception("Canary Resume 실패")
        result.step = STEP_BLOCKED
        result.reason = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        lock.release()
        if owns_connection and conn is not None:
            conn.close()


def format_resume(result: ResumeResult) -> str:
    lines = [
        "=" * 56,
        " DailyReels Targeting Agent — Canary Auto Resume",
        "=" * 56,
    ]
    for key, value in result.as_rows().items():
        lines.append(f" {key:22} : {value}")
    for message in result.messages:
        lines.append(f"   - {message}")
    if result.live is not None:
        lines.append("")
        from .runner import format_result as format_canary

        lines.append(format_canary(result.live))
    else:
        lines.append("=" * 56)
    return "\n".join(lines)
