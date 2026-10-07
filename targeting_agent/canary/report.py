"""Live Canary 보고서 (Phase 18E.58).

Canary가 **실제로 무엇을 했는지**를 DB와 상태 파일만 읽어 결정론적으로 남긴다.
LLM을 부르지 않는다.

이 보고서가 지키는 구분 하나:

    Execution Ground Truth  — 우리가 실제로 눌렀고 눌린 것이 확인됐다
    Human Ground Truth      — 사람이 "이건 좋은 대상이다"라고 말했다

Agent가 누른 것을 사람의 판단으로 올려 적지 않는다. 좋아요를 눌렀다는 사실은
기능이 동작한다는 증거이지, 그 대상이 맞았다는 증거가 아니다.
"""
from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.config import Config
from ..core.logger import get_logger
from ..readiness.guards import effective_mode
from .state import CanaryState, load_state, state_path

logger = get_logger("canary.report")

REPORT_PREFIX = "live_canary_"
LATEST_NAME = "live_canary_latest.html"

# 기술적으로 Canary가 성공했다고 볼 최소 건수(§45).
MIN_CONFIRMED_FOR_GO = 3

TECHNICAL_GO = "GO"
TECHNICAL_CONDITIONAL = "CONDITIONAL_GO"
TECHNICAL_NO_GO = "NO_GO"

STYLE = """
body{font-family:system-ui,"Malgun Gothic",sans-serif;margin:0;padding:24px;color:#1b1b1b;background:#fafafa}
h1{font-size:20px;margin:0 0 2px} h2{font-size:15px;margin:24px 0 8px;color:#222}
.muted{color:#666;font-size:12px}
.verdict{display:inline-block;padding:6px 14px;border-radius:6px;font-size:16px;font-weight:600;margin:8px 0}
.go{background:#e6f4ea;color:#14532d} .cond{background:#fef6e0;color:#7a4b00} .nogo{background:#fde8e8;color:#8a1c1c}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff;margin-top:4px}
th,td{border:1px solid #e3e3e3;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f5f5f5;width:280px;font-weight:600}
ul{margin:6px 0 0 18px;font-size:13px} li{margin:2px 0}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _scalar(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


@dataclass
class CanaryReport:
    """Canary 결과. 기술 검증 결과와 사람 판단을 섞지 않는다."""

    generated_at: str = ""
    state: CanaryState = field(default_factory=CanaryState)
    technical: str = TECHNICAL_CONDITIONAL
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def verdict_class(self) -> str:
        return {
            TECHNICAL_GO: "go",
            TECHNICAL_CONDITIONAL: "cond",
            TECHNICAL_NO_GO: "nogo",
        }[self.technical]


def build_canary_report(
    conn: sqlite3.Connection, config: Config, *, now: Optional[datetime] = None
) -> CanaryReport:
    """DB와 상태 파일만 읽어 Canary 결과를 정리한다."""
    moment = now or datetime.now(timezone.utc)
    state = load_state(state_path(config.data_dir))
    report = CanaryReport(generated_at=moment.isoformat(timespec="seconds"), state=state)

    # --- 실제로 쓴 것 ------------------------------------------------------
    live_likes = _scalar(
        conn,
        "SELECT COUNT(*) FROM interactions "
        "WHERE dry_run = 0 AND success = 1 AND action_type = 'LIKE'",
    )
    live_comments = _scalar(
        conn,
        "SELECT COUNT(*) FROM interactions "
        "WHERE dry_run = 0 AND success = 1 AND action_type = 'COMMENT'",
    )
    live_failures = _scalar(
        conn, "SELECT COUNT(*) FROM interactions WHERE dry_run = 0 AND success = 0"
    )
    canary_approved = _scalar(
        conn, "SELECT COUNT(*) FROM action_queue WHERE approved_by = 'live_canary'"
    )
    operator_approved = _scalar(
        conn, "SELECT COUNT(*) FROM action_queue WHERE approved_by = 'operator'"
    )
    human_feedback = _scalar(conn, "SELECT COUNT(*) FROM feedback")

    mode = effective_mode(config)
    report.sections = {
        "Canary 상태": state.as_rows(),
        "실제 Instagram 쓰기": {
            "LIKE (확인됨)": str(live_likes),
            "COMMENT": f"{live_comments} (Phase 18E에서는 0이어야 한다)",
            "FOLLOW / DM / 저장 / 공유": "0 (구현하지 않음)",
            "실패한 LIVE 시도": str(live_failures),
        },
        "감사": {
            "live_canary 자동 승인": str(canary_approved),
            "운영자 승인": str(operator_approved),
            "실행 Executor": "browser",
            "확인 방법": "click → 상태 변경 → 새로고침 후 유지 확인",
        },
        "Ground Truth": {
            "Execution GT (실제 확인된 LIKE)": str(live_likes),
            "Human GT (사람이 남긴 평가)": str(human_feedback),
            "사람 검토 대기 목록": f"{len(state.review_backlog)}건",
            "주의": "Agent가 누른 것은 사람 판단이 아니다 — GOOD_TARGET으로 세지 않는다.",
        },
        "설정 상태": {
            "autopilot.enabled": str(bool(config.get("autopilot.enabled", False))).lower(),
            "browser_executor.mode": str(config.get("browser_executor.mode", "")),
            "actions.execution.dry_run": str(bool(config.get("actions.execution.dry_run", True))).lower(),
            "scheduler.autopilot": str(bool(config.get("scheduler.autopilot", False))).lower(),
            "Effective": mode.as_line(),
        },
    }

    # --- 판정 --------------------------------------------------------------
    if live_comments:
        report.blockers.append(
            f"실제 COMMENT가 {live_comments}건 기록돼 있습니다 — Phase 18E에서는 0이어야 합니다."
        )
    if state.status.startswith("STOPPED"):
        report.blockers.append(f"Canary가 중단되었습니다: {state.status} ({state.stop_reason}).")
    if live_likes > state.max_total:
        report.blockers.append(
            f"실제 LIKE {live_likes}건이 상한 {state.max_total}건을 넘었습니다."
        )
    if mode.writes_enabled:
        report.blockers.append(
            "설정이 아직 LIVE입니다 — Canary 종료 후에는 DRY_RUN으로 돌아와 있어야 합니다."
        )

    if report.blockers:
        report.technical = TECHNICAL_NO_GO
    elif live_likes >= MIN_CONFIRMED_FOR_GO:
        report.technical = TECHNICAL_GO
    else:
        report.technical = TECHNICAL_CONDITIONAL
        report.notes.append(
            f"확인된 실제 LIKE가 {live_likes}건입니다(기술 판정 권장 기준 {MIN_CONFIRMED_FOR_GO}건) — "
            "표본이 적어 기술 검증을 단정하지 않습니다."
        )

    if human_feedback < 10:
        report.notes.append(
            f"사람 Ground Truth가 {human_feedback}건입니다 — Target 보정 신뢰도는 LOW를 유지합니다. "
            "Canary LIKE를 사람 평가로 환산하지 않습니다."
        )
    report.notes.append(
        "Phase 18E는 COMMENT를 실제로 보내지 않습니다 — LIKE가 잘 됐다고 COMMENT LIVE가 "
        "열리지는 않습니다."
    )
    return report


def render_canary_html(report: CanaryReport) -> str:
    def rows(mapping: Mapping[str, Any]) -> str:
        if not mapping:
            return '<p class="muted">없음</p>'
        body = "".join(
            f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>" for key, value in mapping.items()
        )
        return f"<table>{body}</table>"

    def bullets(items: list[str], empty: str) -> str:
        if not items:
            return f'<p class="muted">{_e(empty)}</p>'
        return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in items) + "</ul>"

    sections = "".join(
        f"<h2>{_e(title)}</h2>{rows(section)}" for title, section in report.sections.items()
    )
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>DailyReels Targeting Agent — Live Canary</title>
<style>{STYLE}</style></head><body>
<h1>DailyReels Targeting Agent — Limited Live Canary</h1>
<p class="muted">{_e(report.generated_at)} · DB·상태 파일만 읽어 생성(LLM 호출 없음)</p>

<div class="verdict {report.verdict_class}">기술 검증: {_e(report.technical)}</div>

<h2>차단 사유</h2>
{bullets(report.blockers, "없음")}

<h2>읽을 때 주의</h2>
{bullets(report.notes, "")}

{sections}

<p class="muted">이 보고서는 설정을 바꾸지 않습니다. LIVE 전환은 운영자가 직접 결정합니다.</p>
</body></html>
"""


def write_canary_report(config: Config, report: CanaryReport, html_text: str) -> Path:
    output_dir = config._resolve_path(config.get("scheduler.summary.output_dir", "data/reports"))
    output_dir.mkdir(parents=True, exist_ok=True)
    name = report.state.canary_id or report.generated_at[:10].replace("-", "")
    path = output_dir / f"{REPORT_PREFIX}{name}.html"
    path.write_text(html_text, encoding="utf-8")
    try:
        (output_dir / LATEST_NAME).write_text(html_text, encoding="utf-8")
    except OSError:  # pragma: no cover - 파일 잠김 등
        logger.warning("live_canary_latest.html 생성 실패: %s", output_dir / LATEST_NAME)
    logger.info("Live Canary Report 생성: %s", path)
    return path


def format_canary_report(report: CanaryReport) -> str:
    lines = [
        "=" * 56,
        " Live Canary — 기술 검증 결과",
        "=" * 56,
        f" 판정          : {report.technical}",
        f" Canary 상태   : {report.state.status}",
        f" 확인된 LIKE   : {report.state.total_live_likes} / {report.state.max_total}",
    ]
    for item in report.blockers:
        lines.append(f"   [차단] {item}")
    for item in report.notes:
        lines.append(f"   - {item}")
    lines.append("=" * 56)
    return "\n".join(lines)
