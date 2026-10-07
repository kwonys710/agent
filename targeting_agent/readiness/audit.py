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
    # interactions 테이블에서 직접 센 실제 쓰기. Action에 연결되지 않은 기록이
    # 있어도 놓치지 않기 위해 Action을 따라가지 않고 따로 센다.
    recorded_real_writes: int = 0
    # 어느 Action에도 연결되지 않은 실행 기록. 있으면 "누가 시켰는지" 알 수 없다.
    orphan_interactions: int = 0

    @property
    def total(self) -> int:
        return len(self.traces)

    @property
    def executed(self) -> int:
        return sum(1 for trace in self.traces if trace.executed)

    @property
    def real_writes(self) -> int:
        """실제 Instagram 쓰기 수.

        Action을 따라가며 센 수와 interactions에서 직접 센 수 중 **큰 쪽**을 쓴다.
        Action에 연결되지 않은 기록이 있어도 '쓰기 0건'으로 보고하면 안 된다.
        """
        traced = sum(1 for trace in self.traces if trace.real_write)
        return max(traced, self.recorded_real_writes)

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
            "Action에 연결 안 된 실행 기록": str(self.orphan_interactions),
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

    # Action을 따라가지 않고 interactions를 직접 본다 — 연결이 끊긴 기록도 세기 위함.
    report.recorded_real_writes = int(
        conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE dry_run = 0 AND success = 1"
        ).fetchone()[0]
    )
    report.orphan_interactions = int(
        conn.execute(
            """SELECT COUNT(*) FROM interactions i
               WHERE i.action_id IS NULL
                  OR NOT EXISTS (SELECT 1 FROM action_queue q WHERE q.action_id = i.action_id)"""
        ).fetchone()[0]
    )
    if report.orphan_interactions:
        report.issues.append(
            f"실행 기록 {report.orphan_interactions}건이 어느 Action에도 연결돼 있지 않습니다 "
            "— 누가 시켰는지 되짚을 수 없습니다."
        )

    _check_issues(report)
    return report


# --- 후보부터 실행까지 한 줄로 잇기 (Phase 18D.6) ----------------------------
_CHAIN_SQL = """
SELECT
    q.action_id, q.action_type, q.status, q.approved_by, q.comment_text,
    m.media_pk, m.media_id, m.canonical_url, m.permalink, m.target_score,
    (SELECT COUNT(*) FROM media_analysis a WHERE a.media_pk = m.media_pk
        AND a.target_score IS NOT NULL)                                   AS analyses,
    (SELECT COUNT(*) FROM comment_drafts d WHERE d.media_pk = m.media_pk
        AND d.quality_ok = 1)                                             AS passed_drafts,
    (SELECT COUNT(*) FROM interactions i WHERE i.action_id = q.action_id) AS interactions
FROM action_queue q
LEFT JOIN candidate_media m ON m.media_pk = q.media_pk
{where}
ORDER BY q.action_id
"""

# run_id는 **Action을 만든 Run**이다. 어떤 Run이 실행한 Action은 그보다 앞선
# Run에서 만들어졌을 수 있어서, 실행된 것을 보려면 따로 골라야 한다.
_WHERE_RUN = "WHERE q.run_id = ?"
_WHERE_EXECUTED = (
    "WHERE EXISTS (SELECT 1 FROM interactions i WHERE i.action_id = q.action_id)"
)


@dataclass
class ChainLink:
    """후보 한 건이 Action까지 이어진 경로."""

    action_id: int
    action_type: str
    media_id: str
    canonical_url: str
    target_score: Optional[float]
    has_analysis: bool
    has_comment_draft: bool
    approved_by: str
    executed: bool
    broken: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.broken


def collect_chain(
    conn: sqlite3.Connection, run_id: Optional[str] = None
) -> list[ChainLink]:
    """**후보 → 분석 → 댓글 → Action → 실행**이 이어지는지 본다.

    감사에서 중요한 것은 각 단계가 존재하는지가 아니라 **서로 연결돼 있는지**다.
    중간 고리가 비어 있으면 나중에 "왜 이게 나갔는지"를 설명할 수 없다.

    `run_id`를 주면 그 Run이 **만든** Action을, 주지 않으면 **실행된** Action을
    본다. 한 Run이 실행하는 Action은 앞선 Run에서 만들어졌을 수 있다.
    """
    links: list[ChainLink] = []
    where, params = (
        (_WHERE_RUN, (run_id,)) if run_id is not None else (_WHERE_EXECUTED, ())
    )
    cursor = conn.execute(_CHAIN_SQL.format(where=where), params)
    columns = [description[0] for description in cursor.description]
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        broken: list[str] = []
        if row["media_pk"] is None:
            broken.append("candidate_missing")
        if not int(row["analyses"] or 0):
            broken.append("analysis_missing")
        # Executor는 canonical_url이 없으면 permalink로 연다(실제 동작과 같은 기준).
        # 둘 다 없을 때만 '열 주소가 없다'로 본다 — 검사가 실제보다 엄하면
        # 멀쩡한 Action을 끊긴 것으로 보고하게 된다.
        if not (str(row["canonical_url"] or "").strip() or str(row["permalink"] or "").strip()):
            broken.append("target_url_missing")
        if row["target_score"] is None:
            broken.append("score_missing")
        if str(row["action_type"]) == "COMMENT":
            if not int(row["passed_drafts"] or 0):
                broken.append("comment_draft_missing")
            if row["status"] != "PENDING" and not str(row["comment_text"] or "").strip():
                broken.append("comment_text_missing")
        if row["status"] in ("SUCCESS", "FAILED") and not int(row["interactions"] or 0):
            broken.append("interaction_missing")
        links.append(
            ChainLink(
                action_id=int(row["action_id"]),
                action_type=str(row["action_type"] or ""),
                media_id=str(row["media_id"] or ""),
                canonical_url=str(row["canonical_url"] or row["permalink"] or ""),
                target_score=row["target_score"],
                has_analysis=bool(int(row["analyses"] or 0)),
                has_comment_draft=bool(int(row["passed_drafts"] or 0)),
                approved_by=str(row["approved_by"] or UNKNOWN),
                executed=bool(int(row["interactions"] or 0)),
                broken=tuple(broken),
            )
        )
    return links


def _check_issues(report: AuditReport) -> None:
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
