"""Operational Baseline Snapshot (Phase 18D.0).

LIVE 준비도를 판단하기 전에 **지금 상태가 어떤지**부터 숫자로 남긴다.
DB만 읽는다(LLM 호출 없음, 설정 변경 없음, Instagram 쓰기 없음).

기간은 두 가지를 함께 본다 — 최근 N일과 전체 운영 기간.
운영 초기에는 최근 7일 표본이 너무 적어 "최근 7일만" 보면 판단을 그르친다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

BROWSER_SOURCE = "instagram_browser_search"
UNKNOWN_APPROVER = "(미기록)"


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return (numerator / denominator) if denominator else None


def pct(value: Optional[float]) -> str:
    """비율을 사람이 읽는 문자열로. 표본이 없으면 '-' 다(0%가 아니다)."""
    return "-" if value is None else f"{value * 100:.1f}%"


def _join(mapping: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(mapping.items())) or "없음"


def _scalar(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _pairs(conn: sqlite3.Connection, sql: str, *params: object) -> dict[str, int]:
    return {str(key): int(count) for key, count in conn.execute(sql, params).fetchall()}


@dataclass
class CandidateCounts:
    """후보 모집단."""

    total: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    browser_total: int = 0
    needs_enrichment: int = 0
    unresolved_creator: int = 0

    def as_rows(self) -> dict[str, str]:
        return {
            "후보 전체": str(self.total),
            "Browser Discovery 후보": str(self.browser_total),
            "Source별": _join(self.by_source),
            "Status별": _join(self.by_status),
            "NEEDS_ENRICHMENT": str(self.needs_enrichment),
            "Creator 미확인(unresolved)": str(self.unresolved_creator),
        }


@dataclass
class ExtractionCounts:
    """Browser Discovery 추출 품질(browser_discovery_runs 집계)."""

    runs: int = 0
    failed_runs: int = 0
    queries: int = 0
    found: int = 0
    collected: int = 0
    added: int = 0
    duplicate: int = 0
    username_found: int = 0
    caption_found: int = 0
    detail_failed: int = 0
    selector_errors: int = 0
    needs_enrichment: int = 0

    @property
    def username_rate(self) -> Optional[float]:
        return _ratio(self.username_found, self.collected)

    @property
    def caption_rate(self) -> Optional[float]:
        return _ratio(self.caption_found, self.collected)

    @property
    def duplicate_rate(self) -> Optional[float]:
        return _ratio(self.duplicate, self.added + self.duplicate)

    @property
    def run_failure_rate(self) -> Optional[float]:
        return _ratio(self.failed_runs, self.runs)

    @property
    def selector_error_rate(self) -> Optional[float]:
        """검색어 하나당 selector 오류 비율. 검색어가 없으면 측정하지 않는다."""
        return _ratio(self.selector_errors, self.queries)

    def as_rows(self) -> dict[str, str]:
        return {
            "Discovery 실행": f"{self.runs} (실패 {self.failed_runs} · {pct(self.run_failure_rate)})",
            "검색어": str(self.queries),
            "발견 / 수집": f"{self.found} / {self.collected}",
            "신규 / 중복": f"{self.added} / {self.duplicate} ({pct(self.duplicate_rate)})",
            "Username 추출률": f"{pct(self.username_rate)} ({self.username_found}/{self.collected})",
            "Caption 추출률": f"{pct(self.caption_rate)} ({self.caption_found}/{self.collected})",
            "상세 읽기 실패": str(self.detail_failed),
            "Selector 오류": f"{self.selector_errors} ({pct(self.selector_error_rate)} / 검색어)",
            "NEEDS_ENRICHMENT": str(self.needs_enrichment),
        }


@dataclass
class AiCounts:
    """AI 분석 상태."""

    analyzed: int = 0
    claude_rows: int = 0
    heuristic_rows: int = 0
    claude_calls: int = 0
    cache_hits: int = 0
    fallbacks: int = 0
    cache_entries: int = 0

    @property
    def cache_hit_rate(self) -> Optional[float]:
        return _ratio(self.cache_hits, self.claude_calls + self.cache_hits)

    @property
    def fallback_rate(self) -> Optional[float]:
        return _ratio(self.fallbacks, max(self.claude_calls + self.cache_hits, self.fallbacks))

    def as_rows(self) -> dict[str, str]:
        return {
            "분석 레코드": f"{self.analyzed} (claude {self.claude_rows} / heuristic {self.heuristic_rows})",
            "Claude 호출 / Cache Hit": f"{self.claude_calls} / {self.cache_hits}",
            "Cache Hit 비율": pct(self.cache_hit_rate),
            "Heuristic Fallback": f"{self.fallbacks} ({pct(self.fallback_rate)})",
            "Cache 적재": str(self.cache_entries),
        }


@dataclass
class CommentCounts:
    """댓글 생성/품질 게이트."""

    total: int = 0
    passed: int = 0
    rejected: int = 0
    by_generator: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    reject_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def reject_rate(self) -> Optional[float]:
        return _ratio(self.rejected, self.total)

    @property
    def template_rate(self) -> Optional[float]:
        return _ratio(self.by_generator.get("template", 0), self.total)

    def as_rows(self) -> dict[str, str]:
        return {
            "생성 댓글": str(self.total),
            "품질 통과 / 거절": f"{self.passed} / {self.rejected} ({pct(self.reject_rate)})",
            "생성기별": _join(self.by_generator),
            "상태별": _join(self.by_status),
            "Template 비율": pct(self.template_rate),
            "거절 사유": _join(self.reject_reasons),
        }


@dataclass
class ActionCounts:
    """Action Queue / 승인 / 실행 / 감사."""

    queued: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    approved_total: int = 0
    approved_by: dict[str, int] = field(default_factory=dict)
    interactions: int = 0
    dry_run_interactions: int = 0
    real_writes: int = 0
    executed_success: int = 0
    executed_failed: int = 0
    human_feedback: int = 0
    feedback_by_type: dict[str, int] = field(default_factory=dict)

    def as_rows(self) -> dict[str, str]:
        return {
            "Action Queue": f"{self.queued} ({_join(self.by_status)})",
            "종류별": _join(self.by_type),
            "승인": f"{self.approved_total} ({_join(self.approved_by)})",
            "Interaction 기록": f"{self.interactions} (DRY_RUN {self.dry_run_interactions})",
            "실제 Instagram 쓰기": str(self.real_writes),
            "실행 성공 / 실패": f"{self.executed_success} / {self.executed_failed}",
            "사람 Feedback": f"{self.human_feedback} ({_join(self.feedback_by_type)})",
        }


@dataclass
class BaselineSnapshot:
    """Phase 18D.0 기준선. 모든 수치는 DB에서 직접 센 것이다."""

    generated_at: str = ""
    window_days: int = 7
    window_start: str = ""
    window_end: str = ""
    candidates: CandidateCounts = field(default_factory=CandidateCounts)
    extraction_window: ExtractionCounts = field(default_factory=ExtractionCounts)
    extraction_all: ExtractionCounts = field(default_factory=ExtractionCounts)
    ai: AiCounts = field(default_factory=AiCounts)
    comments: CommentCounts = field(default_factory=CommentCounts)
    actions: ActionCounts = field(default_factory=ActionCounts)
    notes: list[str] = field(default_factory=list)

    def as_sections(self) -> dict[str, dict[str, str]]:
        return {
            "후보 모집단 (전체 기간)": self.candidates.as_rows(),
            f"Discovery / 추출 (최근 {self.window_days}일)": self.extraction_window.as_rows(),
            "Discovery / 추출 (전체 기간)": self.extraction_all.as_rows(),
            "AI 분석 (전체 기간)": self.ai.as_rows(),
            "댓글 품질 (전체 기간)": self.comments.as_rows(),
            "Action / 감사 (전체 기간)": self.actions.as_rows(),
        }


# browser_discovery_runs의 선택 지표 — (필드 이름, 실제 컬럼 이름).
# 스키마 버전에 따라 없을 수 있으므로 없으면 0으로 둔다(없는 것을 실패로 보지 않는다).
_OPTIONAL_COLUMNS = (
    ("username_found", "username_found"),
    ("caption_found", "caption_found"),
    ("detail_failed", "detail_failed"),
    ("selector_errors", "errors"),
    ("needs_enrichment", "needs_enrichment"),
)


def _collect_extraction(conn: sqlite3.Connection, *, since: Optional[str] = None) -> ExtractionCounts:
    where = "WHERE started_at >= ?" if since else ""
    params: tuple[object, ...] = (since,) if since else ()
    row = conn.execute(
        f"""SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN status IN ('FAILED','SESSION_STOPPED') THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(queries),0),
                   COALESCE(SUM(found),0),
                   COALESCE(SUM(collected),0),
                   COALESCE(SUM(added),0),
                   COALESCE(SUM(duplicate),0)
            FROM browser_discovery_runs {where}""",
        params,
    ).fetchone()
    counts = ExtractionCounts(
        runs=int(row[0] or 0),
        failed_runs=int(row[1] or 0),
        queries=int(row[2] or 0),
        found=int(row[3] or 0),
        collected=int(row[4] or 0),
        added=int(row[5] or 0),
        duplicate=int(row[6] or 0),
    )
    for attribute, column in _OPTIONAL_COLUMNS:
        try:
            value = _scalar(
                conn,
                f"SELECT COALESCE(SUM({column}),0) FROM browser_discovery_runs {where}",
                *params,
            )
        except sqlite3.OperationalError:
            continue  # 예전 스키마 — 없는 지표는 0으로 둔다(실패로 보지 않는다)
        setattr(counts, attribute, value)
    return counts


_REJECT_FAMILY_SQL = """
SELECT reason_family, COUNT(*) FROM (
    SELECT CASE
        WHEN INSTR(quality_reason, '(') > 0
            THEN SUBSTR(quality_reason, 1, INSTR(quality_reason, '(') - 1)
        WHEN INSTR(quality_reason, ':') > 0
            THEN SUBSTR(quality_reason, 1, INSTR(quality_reason, ':') - 1)
        ELSE quality_reason
    END AS reason_family
    FROM comment_drafts
    WHERE quality_ok = 0 AND quality_reason <> ''
) GROUP BY reason_family
"""

_UNRESOLVED_SQL = """
SELECT COUNT(*) FROM candidate_media m
LEFT JOIN creators c ON c.creator_id = m.creator_id
WHERE c.username IS NULL OR c.username LIKE 'unresolved:%'
"""

_APPROVED_BY_SQL = """
SELECT COALESCE(approved_by, ?), COUNT(*) FROM action_queue
WHERE approved_at IS NOT NULL GROUP BY approved_by
"""


def collect_baseline(
    conn: sqlite3.Connection,
    *,
    now: Optional[datetime] = None,
    window_days: int = 7,
    minimum_window_samples: int = 5,
) -> BaselineSnapshot:
    """운영 기준선을 모은다. **쓰기를 하지 않는다.**"""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=window_days)).date().isoformat()

    snapshot = BaselineSnapshot(
        generated_at=now.isoformat(timespec="seconds"),
        window_days=window_days,
        window_start=start,
        window_end=now.date().isoformat(),
    )

    snapshot.candidates = CandidateCounts(
        total=_scalar(conn, "SELECT COUNT(*) FROM candidate_media"),
        by_source=_pairs(conn, "SELECT source, COUNT(*) FROM candidate_media GROUP BY source"),
        by_status=_pairs(conn, "SELECT status, COUNT(*) FROM candidate_media GROUP BY status"),
        browser_total=_scalar(
            conn, "SELECT COUNT(*) FROM candidate_media WHERE source = ?", BROWSER_SOURCE
        ),
        needs_enrichment=_scalar(
            conn, "SELECT COUNT(*) FROM candidate_media WHERE status = 'NEEDS_ENRICHMENT'"
        ),
        unresolved_creator=_scalar(conn, _UNRESOLVED_SQL),
    )

    snapshot.extraction_window = _collect_extraction(conn, since=start)
    snapshot.extraction_all = _collect_extraction(conn)

    stats = _pairs(conn, "SELECT metric, COALESCE(SUM(value),0) FROM daily_stats GROUP BY metric")
    snapshot.ai = AiCounts(
        analyzed=_scalar(conn, "SELECT COUNT(*) FROM media_analysis"),
        claude_rows=_scalar(
            conn, "SELECT COUNT(*) FROM media_analysis WHERE analyzer = 'claude_code'"
        ),
        heuristic_rows=_scalar(
            conn, "SELECT COUNT(*) FROM media_analysis WHERE analyzer = 'heuristic'"
        ),
        claude_calls=stats.get("claude_requests", 0),
        cache_hits=stats.get("claude_cache_hits", 0),
        fallbacks=stats.get("claude_fallbacks", 0),
        cache_entries=_scalar(conn, "SELECT COUNT(*) FROM ai_analysis_cache"),
    )

    snapshot.comments = CommentCounts(
        total=_scalar(conn, "SELECT COUNT(*) FROM comment_drafts"),
        passed=_scalar(conn, "SELECT COUNT(*) FROM comment_drafts WHERE quality_ok = 1"),
        rejected=_scalar(conn, "SELECT COUNT(*) FROM comment_drafts WHERE quality_ok = 0"),
        by_generator=_pairs(
            conn, "SELECT generator, COUNT(*) FROM comment_drafts GROUP BY generator"
        ),
        by_status=_pairs(conn, "SELECT status, COUNT(*) FROM comment_drafts GROUP BY status"),
        reject_reasons=_pairs(conn, _REJECT_FAMILY_SQL),
    )

    snapshot.actions = ActionCounts(
        queued=_scalar(conn, "SELECT COUNT(*) FROM action_queue"),
        by_status=_pairs(conn, "SELECT status, COUNT(*) FROM action_queue GROUP BY status"),
        by_type=_pairs(conn, "SELECT action_type, COUNT(*) FROM action_queue GROUP BY action_type"),
        approved_total=_scalar(
            conn, "SELECT COUNT(*) FROM action_queue WHERE approved_at IS NOT NULL"
        ),
        approved_by=_pairs(conn, _APPROVED_BY_SQL, UNKNOWN_APPROVER),
        interactions=_scalar(conn, "SELECT COUNT(*) FROM interactions"),
        dry_run_interactions=_scalar(conn, "SELECT COUNT(*) FROM interactions WHERE dry_run = 1"),
        real_writes=_scalar(
            conn, "SELECT COUNT(*) FROM interactions WHERE dry_run = 0 AND success = 1"
        ),
        executed_success=_scalar(conn, "SELECT COUNT(*) FROM interactions WHERE success = 1"),
        executed_failed=_scalar(conn, "SELECT COUNT(*) FROM interactions WHERE success = 0"),
        human_feedback=_scalar(conn, "SELECT COUNT(*) FROM feedback"),
        feedback_by_type=_pairs(
            conn, "SELECT feedback_type, COUNT(*) FROM feedback GROUP BY feedback_type"
        ),
    )

    if snapshot.extraction_window.collected < minimum_window_samples:
        snapshot.notes.append(
            f"최근 {window_days}일 수집 표본이 {snapshot.extraction_window.collected}건입니다 — "
            "비율 지표는 전체 기간 수치를 함께 보세요."
        )
    if snapshot.actions.real_writes:
        snapshot.notes.append(
            f"실제 Instagram 쓰기가 {snapshot.actions.real_writes}건 기록돼 있습니다 — "
            "DRY_RUN 전제를 확인하세요."
        )
    if snapshot.actions.human_feedback == 0:
        snapshot.notes.append("사람 Feedback이 없습니다 — Target 보정의 Ground Truth가 없습니다.")
    return snapshot
