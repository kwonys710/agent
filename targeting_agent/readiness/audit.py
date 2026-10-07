"""Audit Trail Consistency (Phase 18D.1).

LIVE 이후 "누가 무엇을 왜 실행했는가"를 되짚을 수 있어야 한다. 그 정보가
이미 DB에 다 있는지 확인하고, **헷갈리는 표현만** 바로잡는다.

핵심 구분(실기에서 혼동이 있었던 지점):

- `action_queue.executor`   = Queue에 넣을 때 **예정했던** Executor(Planned)
- `interactions.executor`   = 실제로 **실행한** Executor(Actual)

두 값은 다를 수 있고, 다른 것이 정상이다. Autopilot은 Queue 생성과 무관하게
Browser Executor로 실행하기 때문이다. 그래서 둘을 한 칸에 섞어 쓰지 않고
Planned / Actual로 나눠 보여 준다 — schema를 늘리지 않고 표현만 고친다.

생성 주체(created_by)도 새 컬럼 없이 구한다. `action_queue.run_id`가
`autopilot_runs.run_id`에 있으면 Autopilot이 만든 것이고, 아니면 운영자 경로다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

CREATED_BY_AUTOPILOT = "autopilot"
CREATED_BY_PIPELINE = "pipeline"
NOT_EXECUTED = "-"
UNKNOWN = "(미기록)"

# 실행 전/중/후를 한 줄로 보는 감사 질의.
# interactions는 Action 하나에 여러 건이 남을 수 있어(재시도 등) 마지막 것만 본다.
_TRACE_SQL = """
SELECT
    q.action_id          AS action_id,
    q.run_id             AS run_id,
    q.media_pk           AS media_pk,
    q.action_type        AS action_type,
    q.status             AS status,
    q.executor           AS planned_executor,
    q.dry_run            AS planned_dry_run,
    q.approved_by        AS approved_by,
    q.approved_at        AS approved_at,
    q.started_at         AS started_at,
    q.finished_at        AS finished_at,
    q.error              AS error,
    (SELECT i.executor FROM interactions i
      WHERE i.action_id = q.action_id ORDER BY i.interaction_id DESC LIMIT 1) AS actual_executor,
    (SELECT i.dry_run FROM interactions i
      WHERE i.action_id = q.action_id ORDER BY i.interaction_id DESC LIMIT 1) AS actual_dry_run,
    (SELECT i.success FROM interactions i
      WHERE i.action_id = q.action_id ORDER BY i.interaction_id DESC LIMIT 1) AS actual_success,
    (SELECT COUNT(*) FROM autopilot_runs a WHERE a.run_id = q.run_id)         AS autopilot_run
FROM action_queue q
ORDER BY q.action_id
"""


@dataclass
class AuditTrace:
    """Action 하나의 감사 기록."""

    action_id: int
    action_type: str
    status: str
    created_by: str
    approved_by: str
    planned_executor: str
    actual_executor: str
    planned_dry_run: bool
    actual_dry_run: Optional[bool]
    started_at: str = ""
    finished_at: str = ""
    error: str = ""

    @property
    def executed(self) -> bool:
        return self.actual_executor != NOT_EXECUTED

    @property
    def real_write(self) -> bool:
        """실제 Instagram 쓰기였는지. 실행 기록이 없으면 False."""
        return self.executed and self.actual_dry_run is False

    def as_row(self) -> dict[str, str]:
        return {
            "action_id": str(self.action_id),
            "종류": self.action_type,
            "상태": self.status,
            "생성": self.created_by,
            "승인": self.approved_by,
            "Planned Executor": self.planned_executor,
            "Actual Executor": self.actual_executor,
            "DRY_RUN(예정/실제)": (
                f"{'Y' if self.planned_dry_run else 'N'}/"
                f"{NOT_EXECUTED if self.actual_dry_run is None else ('Y' if self.actual_dry_run else 'N')}"
            ),
            "시작": self.started_at or NOT_EXECUTED,
            "종료": self.finished_at or NOT_EXECUTED,
            "오류": self.error or "",
        }


@dataclass
class AuditReport:
    """감사 추적 일관성 결과."""

    traces: list[AuditTrace] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.traces)

    @property
    def executed(self) -> int:
        return sum(1 for trace in self.traces if trace.executed)

    @property
    def real_writes(self) -> int:
        return sum(1 for trace in self.traces if trace.real_write)

    @property
    def unattributed_approvals(self) -> int:
        """승인됐는데 승인 주체가 기록되지 않은 건(감사 공백)."""
        return sum(
            1
            for trace in self.traces
            if trace.approved_by == UNKNOWN and trace.status != "PENDING"
        )

    @property
    def executor_mismatches(self) -> int:
        """예정 Executor와 실제 Executor가 다른 건. **오류가 아니라 정상일 수 있다.**"""
        return sum(
            1
            for trace in self.traces
            if trace.executed and trace.planned_executor != trace.actual_executor
        )

    @property
    def consistent(self) -> bool:
        """감사에 공백이 없는지. Planned/Actual 차이는 공백이 아니다."""
        return not self.issues

    def as_rows(self) -> dict[str, str]:
        return {
            "Action 기록": str(self.total),
            "실행된 Action": str(self.executed),
            "실제 Instagram 쓰기": str(self.real_writes),
            "Planned≠Actual Executor": f"{self.executor_mismatches} (정상일 수 있음 — 표기만 분리)",
            "승인 주체 미기록": str(self.unattributed_approvals),
            "감사 공백": "없음" if self.consistent else f"{len(self.issues)}건",
        }


def collect_audit(conn: sqlite3.Connection) -> AuditReport:
    """Action 전체의 감사 기록을 모은다. **쓰기를 하지 않는다.**"""
    report = AuditReport()
    cursor = conn.execute(_TRACE_SQL)
    columns = [description[0] for description in cursor.description]
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        actual_dry_run = row["actual_dry_run"]
        trace = AuditTrace(
            action_id=int(row["action_id"]),
            action_type=str(row["action_type"] or ""),
            status=str(row["status"] or ""),
            created_by=(
                CREATED_BY_AUTOPILOT if int(row["autopilot_run"] or 0) else CREATED_BY_PIPELINE
            ),
            approved_by=str(row["approved_by"] or UNKNOWN),
            planned_executor=str(row["planned_executor"] or UNKNOWN),
            actual_executor=str(row["actual_executor"] or NOT_EXECUTED),
            planned_dry_run=bool(row["planned_dry_run"]),
            actual_dry_run=None if actual_dry_run is None else bool(actual_dry_run),
            started_at=str(row["started_at"] or ""),
            finished_at=str(row["finished_at"] or ""),
            error=str(row["error"] or ""),
        )
        report.traces.append(trace)

    for trace in report.traces:
        if trace.status in ("SUCCESS", "FAILED") and not trace.executed:
            report.issues.append(
                f"action {trace.action_id}: 상태가 {trace.status}인데 실행 기록(interactions)이 없습니다."
            )
        if trace.executed and trace.approved_by == UNKNOWN:
            report.issues.append(
                f"action {trace.action_id}: 실행됐는데 승인 주체가 기록되지 않았습니다."
            )
        if trace.executed and not trace.finished_at:
            report.issues.append(f"action {trace.action_id}: 실행 종료 시각이 비어 있습니다.")
        if trace.status == "FAILED" and not trace.error:
            report.issues.append(f"action {trace.action_id}: 실패인데 실패 사유가 비어 있습니다.")
    return report
