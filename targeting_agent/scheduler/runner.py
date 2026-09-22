"""Scheduled Run 실행기 (Phase 15).

한 번 실행되면 아래를 하고 종료한다(대기 루프 없음 — 시간 스케줄은 Windows Task Scheduler가 담당).

  lock → inbox 처리 → 신규 후보 분석 → 통계 갱신 → Daily Summary HTML → lock 해제

지키는 것:
- **Action Executor를 실행하지 않는다.** 승인된 Action이 있어도 Scheduled Run은 실행하지 않는다.
- 이미 분석된 후보는 다시 Claude로 보내지 않는다(상태 필터 + Phase 13 캐시).
- 후보 1건의 실패가 전체 실행을 중단시키지 않는다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..ai.intelligence import ClaudeIntelligence
from ..analysis.profile_analyzer import load_profile
from ..core.config import Config
from ..core.database import (
    bump_stat,
    fetch_candidates,
    get_connection,
    init_db,
    today_str,
    utc_now,
)
from ..core.logger import get_logger
from ..core.models import MediaStatus
from ..discovery.ingest import CandidateIngestor, ImportSummary
from .lock import SchedulerLock
from .summary import build_summary_data, cleanup_old_reports, render_summary_html, write_summary

logger = get_logger("scheduler.runner")

# Claude 분석 대상 상태. 이미 분석/처리된 상태는 보내지 않는다.
ANALYZABLE_STATUSES = (MediaStatus.NEW.value,)

STATUS_OK = "OK"
STATUS_LOCKED = "LOCKED"
STATUS_FAILED = "FAILED"


@dataclass
class ScheduledRunResult:
    """Scheduled Run 1회 결과."""

    status: str = STATUS_OK
    started_at: str = ""
    finished_at: str = ""
    processed: int = 0
    analyzed: int = 0
    needs_enrichment: int = 0
    errors: int = 0
    claude_calls: int = 0
    cache_hits: int = 0
    fallbacks: int = 0
    summary_path: Optional[str] = None
    message: str = ""
    ingest: Optional[ImportSummary] = None
    details: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """개별 후보 오류는 실패로 보지 않는다. 실행 자체가 실패했을 때만 non-zero."""
        return 1 if self.status == STATUS_FAILED else 0


ProcessorFactory = Callable[[Config, sqlite3.Connection], Any]


def _default_processor(config: Config, conn: sqlite3.Connection) -> Any:
    """후보 처리기를 만든다. Intelligence를 실행 내내 공유해 한도 가드가 동작하게 한다."""
    from ..pipeline import CandidateProcessor

    profile = load_profile(config.profile_path, conn)
    return CandidateProcessor(
        config,
        conn,
        profile=profile,
        intelligence=ClaudeIntelligence(config, profile),
    )


def run_scheduled(
    config: Config,
    conn: Optional[sqlite3.Connection] = None,
    *,
    processor_factory: ProcessorFactory = _default_processor,
    now: Optional[datetime] = None,
) -> ScheduledRunResult:
    """Scheduled Run 1회. 예외를 밖으로 내보내지 않고 결과로 돌려준다."""
    now = now or datetime.now(timezone.utc)
    result = ScheduledRunResult(started_at=now.isoformat(timespec="seconds"))

    if not bool(config.get("scheduler.enabled", True)):
        result.status = STATUS_OK
        result.message = "scheduler.enabled=false — 아무 것도 하지 않았습니다."
        result.finished_at = utc_now()
        return result

    lock = SchedulerLock(
        path=config._resolve_path(config.get("scheduler.lock.path", "data/.scheduler.lock")),
        stale_minutes=int(config.get("scheduler.lock.stale_minutes", 60)),
        enabled=bool(config.get("scheduler.lock.enabled", True)),
    )
    if not lock.acquire(now):
        result.status = STATUS_LOCKED
        result.message = "다른 Scheduled Run이 진행 중이어서 종료합니다."
        result.finished_at = utc_now()
        logger.info(result.message)
        return result

    owns_connection = conn is None
    try:
        if conn is None:
            conn = get_connection(config.db_path)
            init_db(conn)

        tz = int(config.get("actions.daily_limits.timezone_offset_hours", 9))
        date = today_str(tz)

        if bool(config.get("scheduler.process_inbox", True)):
            result.ingest = _process_inbox(config, conn)

        if bool(config.get("scheduler.process_new_candidates", True)):
            _process_candidates(config, conn, result, processor_factory, date)

        _record_daily_stats(conn, date, result)
        summary_path = _write_summary(config, conn, date, result)
        result.summary_path = str(summary_path)
        cleanup_old_reports(config, now)
        _record_run(conn, result)
    except Exception as exc:  # noqa: BLE001 - 실행 자체의 실패만 non-zero로 만든다
        logger.exception("Scheduled Run 실패")
        result.status = STATUS_FAILED
        result.message = str(exc)[:300]
    finally:
        result.finished_at = utc_now()
        lock.release()
        if owns_connection and conn is not None:
            conn.close()

    logger.info(
        "Scheduled Run 종료: status=%s 처리 %d건(분석 %d / 정보부족 %d / 오류 %d), Claude %d회",
        result.status,
        result.processed,
        result.analyzed,
        result.needs_enrichment,
        result.errors,
        result.claude_calls,
    )
    return result


# --- 단계 ----------------------------------------------------------------
def _process_inbox(config: Config, conn: sqlite3.Connection) -> ImportSummary:
    inbox = config._resolve_path(config.get("discovery.inbox.path", "data/inbox"))
    return CandidateIngestor(conn).process_inbox(
        inbox,
        processed_dir=config._resolve_path(
            config.get("discovery.inbox.processed_dir", "data/inbox/processed")
        ),
        failed_dir=config._resolve_path(
            config.get("discovery.inbox.failed_dir", "data/inbox/failed")
        ),
    )


def _process_candidates(
    config: Config,
    conn: sqlite3.Connection,
    result: ScheduledRunResult,
    processor_factory: ProcessorFactory,
    date: str,
) -> None:
    limit = int(config.get("scheduler.max_candidates_per_run", 10))
    rows = fetch_candidates(conn, ANALYZABLE_STATUSES, limit=limit or None)
    if not rows:
        logger.info("분석할 신규 후보가 없습니다 — Claude 호출 없음.")
        return

    processor = processor_factory(config, conn)
    for row in rows:
        media_pk = int(row["media_pk"])
        try:
            outcome = processor.process(media_pk)
        except Exception as exc:  # noqa: BLE001 - 후보 1건 실패가 실행을 멈추지 않게 한다
            logger.exception("후보 처리 실패: media_pk=%s", media_pk)
            conn.rollback()
            result.processed += 1
            result.errors += 1
            result.details.append(f"media_pk={media_pk} ERROR {str(exc)[:80]}")
            continue
        result.processed += 1
        if outcome.status == "ERROR":
            result.errors += 1
            result.details.append(f"media_pk={media_pk} ERROR {outcome.detail[:80]}")
            continue
        if outcome.status == "NEEDS_ENRICHMENT":
            result.needs_enrichment += 1
            continue
        result.analyzed += 1
        if outcome.status == "FALLBACK_ANALYZED":
            result.fallbacks += 1

    usage = getattr(processor.intelligence, "usage", None)
    if usage is not None:
        result.claude_calls = usage.claude_calls
        result.cache_hits = usage.cache_hits
        result.fallbacks += sum(usage.failures.values())


def _record_daily_stats(conn: sqlite3.Connection, date: str, result: ScheduledRunResult) -> None:
    if result.ingest and result.ingest.added:
        bump_stat(conn, date, "discovered", result.ingest.added)
    if result.analyzed:
        bump_stat(conn, date, "analyzed", result.analyzed)
    if result.cache_hits:
        bump_stat(conn, date, "claude_cache_hits", result.cache_hits)
    if result.fallbacks:
        bump_stat(conn, date, "claude_fallbacks", result.fallbacks)
    conn.commit()


def _write_summary(
    config: Config, conn: sqlite3.Connection, date: str, result: ScheduledRunResult
) -> Path:
    if not bool(config.get("scheduler.summary.enabled", True)):
        return Path("")
    ingest = result.ingest
    inbox = {
        "처리한 파일": len(ingest.files_processed) if ingest else 0,
        "실패한 파일": len(ingest.files_failed) if ingest else 0,
        "Added": ingest.added if ingest else 0,
        "Duplicate": ingest.duplicate if ingest else 0,
        "Invalid": ingest.invalid if ingest else 0,
        "Error": ingest.error if ingest else 0,
    }
    run = {
        "시작": result.started_at,
        "종료": utc_now(),
        "처리 후보": result.processed,
        "분석 완료": result.analyzed,
        "정보 부족": result.needs_enrichment,
        "오류": result.errors,
        "Claude 호출": result.claude_calls,
        "Cache Hit": result.cache_hits,
        "Fallback": result.fallbacks,
    }
    data = build_summary_data(conn, config, date, inbox=inbox, run=run)
    return write_summary(config, data, render_summary_html(data))


def _record_run(conn: sqlite3.Connection, result: ScheduledRunResult) -> None:
    conn.execute(
        "INSERT INTO scheduled_runs (started_at, finished_at, status, candidates_processed, "
        "claude_calls, cache_hits, fallbacks, errors, summary_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.started_at,
            utc_now(),
            result.status,
            result.processed,
            result.claude_calls,
            result.cache_hits,
            result.fallbacks,
            result.errors,
            result.summary_path,
        ),
    )
    conn.commit()


def format_result(result: ScheduledRunResult) -> str:
    """CLI 출력."""
    ingest = result.ingest
    lines = [
        "",
        "=" * 52,
        " Scheduled Run",
        "=" * 52,
        f" 상태       : {result.status}",
        f" 시작/종료  : {result.started_at} → {result.finished_at}",
    ]
    if ingest and ingest.has_input:
        lines.append(
            f" Inbox      : 입력 {ingest.input_count} / 추가 {ingest.added} / 중복 {ingest.duplicate}"
            f" / 무효 {ingest.invalid} / 오류 {ingest.error}"
        )
    lines += [
        f" 후보 처리  : {result.processed} (분석 {result.analyzed} / 정보부족 {result.needs_enrichment}"
        f" / 오류 {result.errors})",
        f" Claude     : 호출 {result.claude_calls} / Cache Hit {result.cache_hits}"
        f" / Fallback {result.fallbacks}",
    ]
    if result.summary_path:
        lines.append(f" Summary    : {result.summary_path}")
    if result.message:
        lines.append(f" 메시지     : {result.message}")
    for detail in result.details[:5]:
        lines.append(f"   - {detail}")
    lines.append("=" * 52)
    return "\n".join(lines)
