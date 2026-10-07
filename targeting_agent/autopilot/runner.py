"""Autopilot Orchestration (Phase 18C).

기존 단계를 **새로 만들지 않고 이어 붙인다.**

    lock → Browser Discovery(선택) → inbox → 신규 후보 분석(CandidateProcessor)
    → Action Queue(ActionPolicy) → 자동 승인(AUTOPILOT 모드만)
    → Action 실행(BrowserExecutor, 기본 DRY_RUN) → 통계/Summary → 실행 이력

지키는 것:
- REVIEW 모드(기본)에서는 자동 승인을 하지 않는다 — Approval Gate가 그대로 살아 있다.
- 실제 LIKE/COMMENT 여부는 `browser_executor.mode`가 최종 결정한다(기본 DRY_RUN).
- 한 단계의 실패가 전체를 멈추지 않는다. 단 로그인/Challenge/경고/작업 차단은 즉시 중단한다.
- Claude 호출 한도·캐시를 우회하지 않는다(CandidateProcessor가 관리).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..actions.controller import ActionController
from ..actions.executors import create_executor
from ..actions.policy import ActionPolicy, AutoApproveResult, RunMode, auto_approve, usage_note
from ..actions.queue import ActionQueueBuilder, QueueBuildResult, merge_results
from ..actions.rate_limiter import RateLimiter
from ..core.config import Config
from ..core.database import (
    bump_stat,
    fetch_candidates,
    get_connection,
    init_db,
    today_str,
    utc_now,
)
from ..core.exceptions import BrowserSessionError, TargetingError
from ..core.logger import get_logger
from ..core.models import MediaStatus
from ..discovery.browser_runner import BrowserDiscoveryResult, run_browser_discovery
from ..discovery.ingest import CandidateIngestor, ImportSummary
from ..pipeline import next_run_id
from ..scheduler.lock import SchedulerLock

logger = get_logger("autopilot.runner")

STATUS_OK = "OK"
STATUS_LOCKED = "LOCKED"
STATUS_PARTIAL = "PARTIAL"
STATUS_STOPPED = "STOPPED"
STATUS_FAILED = "FAILED"

ANALYZABLE_STATUSES = (MediaStatus.NEW.value,)
QUEUEABLE_STATUSES = (MediaStatus.SCORED.value,)


@dataclass
class AutopilotResult:
    """Autopilot 1회 실행 결과."""

    status: str = STATUS_OK
    mode: str = RunMode.REVIEW.value
    executor_mode: str = "DRY_RUN"
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    discovery: Optional[BrowserDiscoveryResult] = None
    ingest: Optional[ImportSummary] = None
    analyzed: int = 0
    needs_enrichment: int = 0
    claude_calls: int = 0
    cache_hits: int = 0
    queued_likes: int = 0
    queued_comments: int = 0
    queue_skipped: int = 0
    approved: int = 0
    approval_skipped: int = 0
    executed_success: int = 0
    executed_skipped: int = 0
    executed_failed: int = 0
    awaiting_approval: int = 0
    real_writes: int = 0
    errors: int = 0
    halted: bool = False
    halt_reason: str = ""
    message: str = ""
    usage: str = ""
    stages: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.status in (STATUS_OK, STATUS_PARTIAL, STATUS_LOCKED):
            return 0
        return 1

    def note(self, stage: str) -> None:
        self.stages.append(stage)


ProcessorFactory = Callable[[Config, sqlite3.Connection], Any]


def _default_processor(config: Config, conn: sqlite3.Connection) -> Any:
    from ..ai.intelligence import ClaudeIntelligence
    from ..analysis.profile_analyzer import load_profile
    from ..pipeline import CandidateProcessor

    profile = load_profile(config.profile_path, conn)
    return CandidateProcessor(
        config, conn, profile=profile, intelligence=ClaudeIntelligence(config, profile)
    )


def run_autopilot(
    config: Config,
    conn: Optional[sqlite3.Connection] = None,
    *,
    browser: Optional[Any] = None,
    action_page: Optional[Any] = None,
    processor_factory: ProcessorFactory = _default_processor,
    now: Optional[datetime] = None,
) -> AutopilotResult:
    """Autopilot 1회. 예외를 밖으로 내보내지 않고 결과로 돌려준다."""
    now = now or datetime.now(timezone.utc)
    mode = RunMode.from_config(config)
    result = AutopilotResult(
        mode=mode.value,
        executor_mode=str(config.get("browser_executor.mode", "DRY_RUN")),
        started_at=now.isoformat(timespec="seconds"),
    )

    lock = SchedulerLock(
        path=config._resolve_path(config.get("autopilot.lock.path", "data/.autopilot.lock")),
        stale_minutes=int(config.get("autopilot.lock.stale_minutes", 60)),
        enabled=bool(config.get("autopilot.lock.enabled", True)),
    )
    if not lock.acquire(now):
        result.status = STATUS_LOCKED
        result.message = "다른 Autopilot이 진행 중이어서 종료합니다."
        result.finished_at = utc_now()
        logger.info(result.message)
        return result

    owns_connection = conn is None
    try:
        if conn is None:
            conn = get_connection(config.db_path)
            init_db(conn)
        result.run_id = next_run_id(conn)
        date = today_str(int(config.get("actions.daily_limits.timezone_offset_hours", 9)))

        logger.info(
            "Autopilot 시작 run_id=%s mode=%s executor=%s",
            result.run_id,
            result.mode,
            result.executor_mode,
        )

        _stage_discovery(config, conn, result, browser)
        _stage_inbox(config, conn, result)
        _stage_analyze(config, conn, result, processor_factory)
        _stage_queue(config, conn, result)
        _stage_approve(config, conn, result, mode)
        _stage_execute(config, conn, result, action_page)
        _stage_report(config, conn, result, date)
    except BrowserSessionError as exc:
        result.status = STATUS_STOPPED
        result.halted = True
        result.halt_reason = exc.state
        result.message = str(exc)
        logger.warning("Autopilot 중단(%s): %s", exc.state, exc)
    except TargetingError as exc:
        result.status = STATUS_FAILED
        result.message = str(exc)[:300]
        logger.error("Autopilot 실패: %s", exc)
    except Exception as exc:  # noqa: BLE001 - 실행 자체의 실패만 non-zero
        logger.exception("Autopilot 실패")
        result.status = STATUS_FAILED
        result.message = str(exc)[:300]
    finally:
        result.finished_at = utc_now()
        if conn is not None:
            try:
                result.usage = usage_note(config, conn)
                _record_run(conn, result)
            except sqlite3.Error:  # pragma: no cover - 이력 기록 실패는 무시
                logger.warning("Autopilot 이력 기록 실패(무시)")
        lock.release()
        if owns_connection and conn is not None:
            conn.close()

    if result.status == STATUS_OK and result.errors:
        result.status = STATUS_PARTIAL
    logger.info(
        "Autopilot 종료: status=%s 분석 %d / 큐 %d / 승인 %d / 실행 %d (실제 쓰기 %d)",
        result.status,
        result.analyzed,
        result.queued_likes + result.queued_comments,
        result.approved,
        result.executed_success,
        result.real_writes,
    )
    return result


# --- 단계 ------------------------------------------------------------------
def _stage_discovery(
    config: Config, conn: sqlite3.Connection, result: AutopilotResult, browser: Optional[Any]
) -> None:
    if not bool(config.get("autopilot.discovery", True)):
        result.note("discovery:skipped(설정)")
        return
    if browser is None and not bool(config.get("browser_discovery.enabled", False)):
        result.note("discovery:skipped(browser_discovery.enabled=false)")
        return

    discovery = run_browser_discovery(config, conn, browser=browser, process=False)
    result.discovery = discovery
    result.note(f"discovery:{discovery.status} 신규 {discovery.added}")
    if discovery.status == "SESSION_STOPPED":
        raise BrowserSessionError(discovery.stop_reason or "UNKNOWN", discovery.message)
    result.errors += discovery.errors


def _stage_inbox(config: Config, conn: sqlite3.Connection, result: AutopilotResult) -> None:
    if not bool(config.get("autopilot.process_inbox", True)):
        result.note("inbox:skipped(설정)")
        return
    inbox = config._resolve_path(config.get("discovery.inbox.path", "data/inbox"))
    summary = CandidateIngestor(conn).process_inbox(
        inbox,
        processed_dir=config._resolve_path(
            config.get("discovery.inbox.processed_dir", "data/inbox/processed")
        ),
        failed_dir=config._resolve_path(
            config.get("discovery.inbox.failed_dir", "data/inbox/failed")
        ),
    )
    result.ingest = summary
    result.note(f"inbox:신규 {summary.added} / 중복 {summary.duplicate}")


def _stage_analyze(
    config: Config,
    conn: sqlite3.Connection,
    result: AutopilotResult,
    processor_factory: ProcessorFactory,
) -> None:
    limit = int(config.get("autopilot.max_candidates_per_run", 10))
    rows = fetch_candidates(conn, ANALYZABLE_STATUSES, limit=limit or None)
    if not rows:
        result.note("analyze:대상 없음")
        return

    processor = processor_factory(config, conn)
    for row in rows:
        media_pk = int(row["media_pk"])
        try:
            outcome = processor.process(media_pk)
        except Exception as exc:  # noqa: BLE001 - 후보 1건 실패를 격리
            logger.exception("후보 처리 실패: media_pk=%s", media_pk)
            conn.rollback()
            result.errors += 1
            result.note(f"analyze:media_pk={media_pk} ERROR {str(exc)[:60]}")
            continue
        if outcome.status == "ERROR":
            result.errors += 1
        elif outcome.status == "NEEDS_ENRICHMENT":
            result.needs_enrichment += 1
        else:
            result.analyzed += 1

    usage = getattr(processor.intelligence, "usage", None)
    if usage is not None:
        result.claude_calls = usage.claude_calls
        result.cache_hits = usage.cache_hits
    result.note(f"analyze:분석 {result.analyzed} / 정보부족 {result.needs_enrichment}")


def _stage_queue(config: Config, conn: sqlite3.Connection, result: AutopilotResult) -> None:
    """점수가 매겨진 후보에 Action Queue를 만든다(ActionPolicy 기준)."""
    rows = fetch_candidates(
        conn, QUEUEABLE_STATUSES, limit=int(config.get("autopilot.max_candidates_per_run", 10))
    )
    if not rows:
        result.note("queue:대상 없음")
        return

    builder = ActionQueueBuilder(config, result.run_id)
    outcomes: list[QueueBuildResult] = []
    for row in rows:
        media = dict(row)
        score = float(media.get("target_score") or 0)
        draft_id, comment_text = _best_comment(conn, int(media["media_pk"]))
        try:
            outcomes.append(
                builder.build(conn, media, score, comment_draft_id=draft_id, comment_text=comment_text)
            )
        except Exception as exc:  # noqa: BLE001 - 후보 1건 실패를 격리
            logger.exception("Action Queue 생성 실패: media_pk=%s", media.get("media_pk"))
            conn.rollback()
            result.errors += 1
            result.note(f"queue:media_pk={media.get('media_pk')} ERROR {str(exc)[:60]}")
    conn.commit()

    merged = merge_results(outcomes)
    result.queued_likes = merged.likes
    result.queued_comments = merged.comments
    result.queue_skipped = merged.skipped
    result.note(f"queue:LIKE {merged.likes} / COMMENT {merged.comments} / 보류 {merged.skipped}")


def _best_comment(conn: sqlite3.Connection, media_pk: int) -> tuple[Optional[int], Optional[str]]:
    """저장된 댓글 후보 중 품질 통과한 것을 고른다(새로 생성하지 않는다)."""
    row = conn.execute(
        "SELECT draft_id, text FROM comment_drafts "
        "WHERE media_pk = ? AND quality_ok = 1 "
        "ORDER BY (status = 'SELECTED') DESC, COALESCE(similarity_max, 0) ASC, draft_id ASC "
        "LIMIT 1",
        (media_pk,),
    ).fetchone()
    if row is None:
        return None, None
    return int(row["draft_id"]), str(row["text"])


def _stage_approve(
    config: Config, conn: sqlite3.Connection, result: AutopilotResult, mode: RunMode
) -> None:
    outcome: AutoApproveResult = auto_approve(
        conn, config, mode=mode, policy=ActionPolicy.from_config(config)
    )
    result.approved = outcome.approved
    result.approval_skipped = outcome.skipped
    if mode is RunMode.AUTOPILOT:
        result.note(f"approve:자동 승인 {outcome.approved} / 보류 {outcome.skipped}")
    else:
        result.note("approve:REVIEW 모드 — 운영자 승인 대기")


def _stage_execute(
    config: Config,
    conn: sqlite3.Connection,
    result: AutopilotResult,
    action_page: Optional[Any],
) -> None:
    if not bool(config.get("autopilot.execute_actions", True)):
        result.note("execute:skipped(설정)")
        return

    executor = create_executor(
        "browser",
        export_dir=config.export_dir,
        run_id=result.run_id,
        dry_run=config.dry_run,
        config=config,
        conn=conn,
        page=action_page,
    )
    controller = ActionController(config, conn, executor, RateLimiter(config, conn))
    summary = controller.run()

    result.executed_success = summary.success
    result.executed_skipped = summary.skipped
    result.executed_failed = summary.failed
    result.awaiting_approval = summary.awaiting_approval
    result.halted = result.halted or summary.halted
    if summary.halted:
        result.halt_reason = result.halt_reason or summary.halt_reason
        result.status = STATUS_STOPPED
    result.real_writes = _count_real_writes(conn, result.run_id)
    result.note(
        f"execute:성공 {summary.success} / 보류 {summary.skipped} / 실패 {summary.failed}"
        f" (실제 쓰기 {result.real_writes})"
    )


def _count_real_writes(conn: sqlite3.Connection, run_id: str) -> int:
    """이번 Run에서 실제(dry_run=0)로 기록된 Interaction 수."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM interactions i "
        "JOIN action_queue a ON a.action_id = i.action_id "
        "WHERE a.run_id = ? AND i.dry_run = 0",
        (run_id,),
    ).fetchone()
    return int(row["n"] if row else 0)


def _stage_report(
    config: Config, conn: sqlite3.Connection, result: AutopilotResult, date: str
) -> None:
    if result.analyzed:
        bump_stat(conn, date, "analyzed", result.analyzed)
    conn.commit()

    if not bool(config.get("autopilot.summary.enabled", True)):
        result.note("report:skipped(설정)")
        return
    from ..scheduler.summary import (
        build_summary_data,
        cleanup_old_reports,
        render_summary_html,
        write_summary,
    )

    run = {
        "모드": result.mode,
        "Executor": result.executor_mode,
        "분석 완료": result.analyzed,
        "정보 부족": result.needs_enrichment,
        "Queue LIKE": result.queued_likes,
        "Queue COMMENT": result.queued_comments,
        "자동 승인": result.approved,
        "실행 성공": result.executed_success,
        "실행 보류": result.executed_skipped,
        "실행 실패": result.executed_failed,
        "실제 쓰기": result.real_writes,
        "Claude 호출": result.claude_calls,
        "Cache Hit": result.cache_hits,
    }
    data = build_summary_data(conn, config, date, run=run)
    path = write_summary(config, data, render_summary_html(data))
    cleanup_old_reports(config, datetime.now(timezone.utc))
    result.note(f"report:{path}")


def _record_run(conn: sqlite3.Connection, result: AutopilotResult) -> None:
    conn.execute(
        "INSERT INTO autopilot_runs (run_id, started_at, finished_at, status, mode, executor_mode, "
        "analyzed, queued_likes, queued_comments, approved, executed_success, executed_skipped, "
        "executed_failed, real_writes, claude_calls, cache_hits, errors, halt_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.run_id,
            result.started_at,
            result.finished_at,
            result.status,
            result.mode,
            result.executor_mode,
            result.analyzed,
            result.queued_likes,
            result.queued_comments,
            result.approved,
            result.executed_success,
            result.executed_skipped,
            result.executed_failed,
            result.real_writes,
            result.claude_calls,
            result.cache_hits,
            result.errors,
            result.halt_reason or None,
        ),
    )
    conn.commit()


def format_result(result: AutopilotResult) -> str:
    """CLI 출력용 요약."""
    lines = [
        "",
        "=" * 56,
        " DailyReels Targeting Agent — Autopilot",
        "=" * 56,
        f" 상태        : {result.status}",
        f" 모드        : {result.mode} (Executor {result.executor_mode})",
        f" Run ID      : {result.run_id}",
        f" 분석        : {result.analyzed} (정보 부족 {result.needs_enrichment})",
        f" Action Queue: LIKE {result.queued_likes} / COMMENT {result.queued_comments}"
        f" (보류 {result.queue_skipped})",
        f" 승인        : 자동 {result.approved} / 대기 {result.awaiting_approval}",
        f" 실행        : 성공 {result.executed_success} / 보류 {result.executed_skipped}"
        f" / 실패 {result.executed_failed}",
        f" 실제 쓰기   : {result.real_writes}",
        f" Claude      : {result.claude_calls}회 (Cache {result.cache_hits})",
        f" 오늘 사용량 : {result.usage or '-'}",
        f" 오류        : {result.errors}",
    ]
    if result.discovery is not None:
        lines.insert(
            7,
            f" Discovery   : {result.discovery.status} 발견 {result.discovery.found}"
            f" / 신규 {result.discovery.added}",
        )
    if result.halted:
        lines.append(f" 중단 사유   : {result.halt_reason}")
    if result.message:
        lines.append(f" 메시지      : {result.message}")
    if result.stages:
        lines.append(" 단계        :")
        lines.extend(f"   - {stage}" for stage in result.stages)
    lines.append("=" * 56)
    return "\n".join(lines)
