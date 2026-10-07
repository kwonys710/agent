"""운영 품질 모니터링 (Phase 18C.1).

Phase 17의 `learning/metrics.py`(검토 품질 / AI·Scheduler 상태)에 더해,
Phase 18에서 생긴 **Discovery / 추출 / Executor / Autopilot** 지표를 모은다.
기존 지표를 다시 계산하지 않는다 — 없는 것만 추가한다.

원칙:
- DB만 읽는다. LLM을 호출하지 않는다(보고서도 결정론적으로 만든다).
- 임계값(LIKE/COMMENT 기준)을 **자동으로 바꾸지 않는다.** 추천 문구만 만든다.
- Action 실패는 품질 신호가 아니라 실행 건강(execution health) 지표로 분류한다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..core.config import Config
from ..core.logger import get_logger

logger = get_logger("learning.operations")


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return (numerator / denominator) if denominator else None


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _scalar(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    value = row[0] if row else 0
    return int(value or 0)


@dataclass
class DiscoveryMetrics:
    """Browser Discovery / 추출 품질."""

    runs: int = 0
    failed_runs: int = 0
    queries: int = 0
    failed_queries: int = 0
    found: int = 0
    collected: int = 0
    added: int = 0
    duplicate: int = 0
    username_found: int = 0
    caption_found: int = 0
    detail_failed: int = 0
    selector_errors: int = 0

    @property
    def selector_failure_rate(self) -> Optional[float]:
        return _ratio(self.failed_queries, self.queries)

    @property
    def username_rate(self) -> Optional[float]:
        return _ratio(self.username_found, self.collected)

    @property
    def caption_rate(self) -> Optional[float]:
        return _ratio(self.caption_found, self.collected)

    @property
    def duplicate_rate(self) -> Optional[float]:
        return _ratio(self.duplicate, self.added + self.duplicate)

    def as_rows(self) -> dict[str, str]:
        return {
            "Discovery 실행": f"{self.runs} (실패 {self.failed_runs})",
            "검색어 실패율": f"{_pct(self.selector_failure_rate)} ({self.failed_queries}/{self.queries})",
            "발견 / 수집": f"{self.found} / {self.collected}",
            "신규 / 중복": f"{self.added} / {self.duplicate} ({_pct(self.duplicate_rate)})",
            "Username 추출률": f"{_pct(self.username_rate)} ({self.username_found}/{self.collected})",
            "Caption 추출률": f"{_pct(self.caption_rate)} ({self.caption_found}/{self.collected})",
            "상세 읽기 실패": str(self.detail_failed),
        }


@dataclass
class ExecutorMetrics:
    """Action 실행 건강 지표(품질 신호가 아니다)."""

    queued: int = 0
    awaiting_approval: int = 0
    executed: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    real_writes: int = 0
    dry_run_writes: int = 0
    autopilot_runs: int = 0
    autopilot_failed_runs: int = 0
    autopilot_halted_runs: int = 0

    @property
    def success_rate(self) -> Optional[float]:
        return _ratio(self.success, self.executed)

    @property
    def failure_rate(self) -> Optional[float]:
        return _ratio(self.failed, self.executed)

    def as_rows(self) -> dict[str, str]:
        return {
            "Action Queue": f"{self.queued} (승인 대기 {self.awaiting_approval})",
            "실행 성공률": f"{_pct(self.success_rate)} ({self.success}/{self.executed})",
            "실행 실패": f"{self.failed} ({_pct(self.failure_rate)})",
            "보류(Skip)": str(self.skipped),
            "실제 쓰기 / Dry Run": f"{self.real_writes} / {self.dry_run_writes}",
            "Autopilot 실행": (
                f"{self.autopilot_runs} (실패 {self.autopilot_failed_runs} / "
                f"중단 {self.autopilot_halted_runs})"
            ),
        }


@dataclass
class OperationsReport:
    """운영 품질 요약(Daily/Weekly 공용)."""

    discovery: DiscoveryMetrics = field(default_factory=DiscoveryMetrics)
    executor: ExecutorMetrics = field(default_factory=ExecutorMetrics)
    score_distribution: dict[str, int] = field(default_factory=dict)
    action_recommendation: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_sections(self) -> dict[str, dict[str, str]]:
        return {
            "Discovery / 추출": self.discovery.as_rows(),
            "Action 실행": self.executor.as_rows(),
            "Score 분포": {band: str(count) for band, count in self.score_distribution.items()},
            "Action 추천 분포": {
                "추천 없음": str(self.action_recommendation.get("none", 0)),
                "LIKE": str(self.action_recommendation.get("like", 0)),
                "LIKE + COMMENT": str(self.action_recommendation.get("like_comment", 0)),
            },
        }


SCORE_BANDS = ((0, 60), (60, 70), (70, 80), (80, 90), (90, 101))


def collect_discovery_metrics(
    conn: sqlite3.Connection, *, since: Optional[str] = None
) -> DiscoveryMetrics:
    """browser_discovery_runs를 집계한다(없는 컬럼은 0으로 본다)."""
    where = "WHERE started_at >= ?" if since else ""
    params: tuple[object, ...] = (since,) if since else ()
    row = conn.execute(
        "SELECT COUNT(*) AS runs, "
        "       SUM(CASE WHEN status IN ('FAILED', 'SESSION_STOPPED') THEN 1 ELSE 0 END) AS failed, "
        "       COALESCE(SUM(queries), 0) AS queries, COALESCE(SUM(found), 0) AS found, "
        "       COALESCE(SUM(collected), 0) AS collected, COALESCE(SUM(added), 0) AS added, "
        "       COALESCE(SUM(duplicate), 0) AS duplicate, COALESCE(SUM(errors), 0) AS errors, "
        "       COALESCE(SUM(username_found), 0) AS username_found, "
        "       COALESCE(SUM(caption_found), 0) AS caption_found, "
        "       COALESCE(SUM(detail_failed), 0) AS detail_failed "
        f"FROM browser_discovery_runs {where}",
        params,
    ).fetchone()
    metrics = DiscoveryMetrics(
        runs=int(row["runs"] or 0),
        failed_runs=int(row["failed"] or 0),
        queries=int(row["queries"] or 0),
        found=int(row["found"] or 0),
        collected=int(row["collected"] or 0),
        added=int(row["added"] or 0),
        duplicate=int(row["duplicate"] or 0),
        username_found=int(row["username_found"] or 0),
        caption_found=int(row["caption_found"] or 0),
        detail_failed=int(row["detail_failed"] or 0),
        selector_errors=int(row["errors"] or 0),
    )
    # 검색어 실패 수는 "오류가 있었던 Run의 selector 오류 합"으로 근사한다.
    metrics.failed_queries = min(metrics.selector_errors, metrics.queries)
    return metrics


def collect_executor_metrics(
    conn: sqlite3.Connection, *, since: Optional[str] = None
) -> ExecutorMetrics:
    """Action Queue / Interaction / Autopilot 이력을 집계한다."""
    metrics = ExecutorMetrics(
        queued=_scalar(conn, "SELECT COUNT(*) FROM action_queue"),
        awaiting_approval=_scalar(
            conn,
            "SELECT COUNT(*) FROM action_queue WHERE status = 'PENDING' AND approved_at IS NULL",
        ),
        success=_scalar(conn, "SELECT COUNT(*) FROM interactions WHERE success = 1"),
        failed=_scalar(conn, "SELECT COUNT(*) FROM action_queue WHERE status = 'FAILED'"),
        skipped=_scalar(conn, "SELECT COUNT(*) FROM action_queue WHERE status = 'SKIPPED'"),
        real_writes=_scalar(conn, "SELECT COUNT(*) FROM interactions WHERE dry_run = 0"),
        dry_run_writes=_scalar(conn, "SELECT COUNT(*) FROM interactions WHERE dry_run = 1"),
    )
    metrics.executed = _scalar(conn, "SELECT COUNT(*) FROM interactions") + metrics.failed

    where = "WHERE started_at >= ?" if since else ""
    params: tuple[object, ...] = (since,) if since else ()
    row = conn.execute(
        "SELECT COUNT(*) AS runs, "
        "       SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed, "
        "       SUM(CASE WHEN status = 'STOPPED' THEN 1 ELSE 0 END) AS halted "
        f"FROM autopilot_runs {where}",
        params,
    ).fetchone()
    metrics.autopilot_runs = int(row["runs"] or 0)
    metrics.autopilot_failed_runs = int(row["failed"] or 0)
    metrics.autopilot_halted_runs = int(row["halted"] or 0)
    return metrics


def score_distribution(conn: sqlite3.Connection) -> dict[str, int]:
    """Target Score 분포(구간별 후보 수)."""
    counts: dict[str, int] = {}
    for low, high in SCORE_BANDS:
        label = f"{low}-{high - 1}"
        counts[label] = _scalar(
            conn,
            "SELECT COUNT(*) FROM candidate_media "
            "WHERE target_score IS NOT NULL AND target_score >= ? AND target_score < ?",
            low,
            high,
        )
    return counts


def collect_operations_report(
    conn: sqlite3.Connection, config: Config, *, since: Optional[str] = None
) -> OperationsReport:
    """운영 품질 요약을 만든다. 설정을 바꾸지 않고 읽기만 한다."""
    from ..actions.policy import ActionPolicy, recommended_for

    report = OperationsReport(
        discovery=collect_discovery_metrics(conn, since=since),
        executor=collect_executor_metrics(conn, since=since),
        score_distribution=score_distribution(conn),
    )
    scores = [
        row[0]
        for row in conn.execute(
            "SELECT target_score FROM candidate_media WHERE target_score IS NOT NULL"
        ).fetchall()
    ]
    report.action_recommendation = recommended_for(ActionPolicy.from_config(config), scores)
    report.notes = build_notes(report, config)
    return report


def build_notes(report: OperationsReport, config: Config) -> list[str]:
    """경고/추천 문구. **설정을 자동으로 바꾸지 않는다**(추천까지만)."""
    notes: list[str] = []
    discovery = report.discovery
    executor = report.executor
    min_samples = int(config.get("learning.health.minimum_samples", 10))

    if discovery.queries >= min_samples and (discovery.selector_failure_rate or 0) >= 0.5:
        notes.append(
            "검색어 실패율이 높습니다 — Instagram 화면 구조가 바뀌었을 수 있습니다. "
            "data/browser_debug 의 stage 스크린샷을 확인하세요."
        )
    if discovery.collected >= min_samples and (discovery.caption_rate or 1) < 0.5:
        notes.append(
            "Caption 추출률이 낮습니다 — caption selector 점검이 필요합니다"
            "(caption이 없는 게시물은 실패로 세지 않습니다)."
        )
    if discovery.collected >= min_samples and (discovery.username_rate or 1) < 0.8:
        notes.append("Username 추출률이 낮습니다 — 상세 화면 selector를 점검하세요.")
    if executor.executed >= min_samples and (executor.failure_rate or 0) >= 0.2:
        notes.append(
            "Action 실행 실패율이 높습니다 — 실행 건강 문제입니다"
            "(후보 품질 지표와 분리해서 보세요)."
        )
    if executor.autopilot_halted_runs:
        notes.append(
            f"Autopilot이 {executor.autopilot_halted_runs}회 중단됐습니다 — "
            "로그인/Challenge/경고/작업 차단 기록을 확인하세요."
        )
    if executor.awaiting_approval >= int(config.get("dashboard.awaiting_warning", 20)):
        notes.append(f"승인 대기 Action이 {executor.awaiting_approval}건입니다 — Review가 밀려 있습니다.")
    return notes
