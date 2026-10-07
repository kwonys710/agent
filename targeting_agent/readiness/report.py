"""Live Readiness Report (Phase 18D.7 · 18D.8).

지금까지 잰 것을 모아 **LIVE로 가도 되는지** 한 등급으로 판정하고,
결정론적 HTML로 남긴다. LLM을 부르지 않는다 — 같은 DB·같은 설정이면 같은 보고서다.

판정은 두 축을 **따로** 본다.

    기술 준비도(Technical)      : 장치가 제대로 동작하는가
    보정 신뢰도(Calibration)    : 누구를 고를지에 대한 근거가 충분한가

사람 Feedback이 적다는 이유만으로 NO_GO를 주지 않는다 — 그건 기능이 망가진
것이 아니라 아직 배우지 못한 것이다. 반대로 안전장치가 하나라도 깨졌으면
다른 지표가 아무리 좋아도 NO_GO다.
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
from .audit import collect_audit, collect_chain
from .calibration import CONFIDENCE_HIGH, CONFIDENCE_INSUFFICIENT, collect_calibration
from .comments import collect_comment_calibration
from .guards import collect_guards, effective_mode
from .projection import collect_projection
from .snapshot import collect_baseline, pct

logger = get_logger("readiness.report")

GO = "GO"
CONDITIONAL_GO = "CONDITIONAL_GO"
NO_GO = "NO_GO"

READY = "READY"
NOT_READY = "NOT_READY"
LOW = "LOW"
HIGH = "HIGH"

REPORT_PREFIX = "live_readiness_"
LATEST_NAME = "live_readiness_latest.html"

# 표본이 이만큼은 돼야 비율로 경고한다(3건 중 1건 실패로 경고하지 않는다).
MIN_SAMPLES = 10
SELECTOR_ERROR_WARNING = 0.10
CAPTION_RATE_WARNING = 0.80
FALLBACK_RATE_WARNING = 0.30
COMMENT_REJECT_WARNING = 0.50

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


@dataclass
class ReadinessReport:
    """Phase 18D 최종 판정."""

    generated_at: str = ""
    verdict: str = CONDITIONAL_GO
    technical: str = NOT_READY
    calibration_confidence: str = CONFIDENCE_INSUFFICIENT
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    rollout: list[str] = field(default_factory=list)
    config_state: dict[str, str] = field(default_factory=dict)

    @property
    def verdict_class(self) -> str:
        return {GO: "go", CONDITIONAL_GO: "cond", NO_GO: "nogo"}[self.verdict]


def _health_rows(baseline, calibration, comments, config: Config) -> tuple[dict[str, str], list[str]]:
    """운영 건강 지표와 경고(표본이 충분할 때만 경고한다 — §42)."""
    extraction = baseline.extraction_all
    rows = {
        "Discovery 실행": f"{extraction.runs} (실패 {extraction.failed_runs})",
        "수집 / 신규 / 중복": f"{extraction.collected} / {extraction.added} / {extraction.duplicate}",
        "Username 추출률": f"{pct(extraction.username_rate)} ({extraction.username_found}/{extraction.collected})",
        "Caption 추출률": f"{pct(extraction.caption_rate)} ({extraction.caption_found}/{extraction.collected})",
        "중복 비율": pct(extraction.duplicate_rate),
        "Selector 오류": f"{extraction.selector_errors} ({pct(extraction.selector_error_rate)} / 검색어)",
        "상세 읽기 실패": str(extraction.detail_failed),
        "NEEDS_ENRICHMENT": str(baseline.candidates.needs_enrichment),
        "Claude 호출 / Cache Hit": f"{baseline.ai.claude_calls} / {baseline.ai.cache_hits}",
        "Heuristic Fallback": f"{baseline.ai.fallbacks} ({pct(baseline.ai.fallback_rate)})",
        "댓글 통과 / 거절": f"{len(comments.passed)} / {len(comments.rejected)} ({pct(comments.reject_rate)})",
        "댓글 caption 근거": pct(comments.grounded_rate),
    }

    warnings: list[str] = []
    if extraction.queries >= MIN_SAMPLES:
        rate = extraction.selector_error_rate
        if rate is not None and rate >= SELECTOR_ERROR_WARNING:
            warnings.append(
                f"Selector 오류 비율이 {pct(rate)}입니다(검색어 {extraction.queries}건 기준)."
            )
    if extraction.collected >= MIN_SAMPLES:
        rate = extraction.caption_rate
        if rate is not None and rate < CAPTION_RATE_WARNING:
            warnings.append(f"Caption 추출률이 {pct(rate)}로 낮습니다.")
    attempts = baseline.ai.claude_calls + baseline.ai.cache_hits
    if attempts >= MIN_SAMPLES:
        rate = baseline.ai.fallback_rate
        if rate is not None and rate >= FALLBACK_RATE_WARNING:
            warnings.append(f"Heuristic Fallback 비율이 {pct(rate)}로 높습니다.")
    if comments.total >= MIN_SAMPLES:
        rate = comments.reject_rate
        if rate is not None and rate >= COMMENT_REJECT_WARNING:
            warnings.append(f"댓글 거절 비율이 {pct(rate)}로 높습니다.")
    return rows, warnings


def _classify(
    report: ReadinessReport,
    *,
    guards,
    audit,
    calibration,
    comments,
    projection,
    health_warnings: list[str],
) -> None:
    """판정. 안전장치가 깨졌으면 다른 지표와 무관하게 NO_GO."""
    # --- 반드시 막아야 하는 것 -------------------------------------------
    for check in guards.failures:
        report.blockers.append(f"안전장치 실패: {check.name} — {check.detail}")
    if not audit.consistent:
        report.blockers.extend(f"감사 공백: {issue}" for issue in audit.issues)
    if audit.real_writes:
        report.blockers.append(
            f"실제 Instagram 쓰기가 {audit.real_writes}건 기록돼 있습니다 — DRY_RUN 전제가 깨졌습니다."
        )
    report.technical = NOT_READY if report.blockers else READY

    # --- 신뢰도 ----------------------------------------------------------
    report.calibration_confidence = calibration.confidence

    # --- 위험 요소(막지는 않지만 알아야 하는 것) --------------------------
    report.risks.extend(health_warnings)
    if calibration.comment_unreachable:
        report.risks.append(
            f"후보 {len(calibration.comment_unreachable)}/{calibration.total}건은 정보 부족으로 "
            f"COMMENT 임계값({calibration.comment_threshold:.0f})에 도달할 수 없습니다."
        )
    # Browser Discovery가 Action으로 이어지지 않는 상태는 projection이
    # 건수까지 넣어 더 구체적으로 알려 준다 — 여기서 또 적지 않는다.
    if projection.current and projection.current.projected_total == 0:
        report.risks.append("현재 설정으로는 하루 예상 Action이 0건입니다.")
    if comments.prompt_change_warranted:
        report.risks.append("댓글 품질이 기준을 벗어났습니다 — Prompt 조정 검토가 필요합니다.")
    if guards.warnings:
        report.risks.extend(f"{check.name}: {check.detail}" for check in guards.warnings)

    # --- 등급 ------------------------------------------------------------
    if report.blockers:
        report.verdict = NO_GO
    elif report.calibration_confidence == CONFIDENCE_HIGH and not report.risks:
        report.verdict = GO
    else:
        report.verdict = CONDITIONAL_GO

    # --- 권고 ------------------------------------------------------------
    report.recommendations.extend(calibration.recommendations)
    report.recommendations.extend(comments.recommendations)
    report.rollout = [
        "Stage 0 — Discovery + 분석 + DRY_RUN (지금 상태). 실제 쓰기 없음.",
        "Stage 1 — LIKE만 제한적으로 LIVE. actions.enable_comment=false 로 두고 "
        "하루 한도를 낮춰 시작합니다.",
        "Stage 2 — LIKE + COMMENT 제한적으로 LIVE. 댓글은 품질 게이트를 통과하고 "
        "중복/한도 검사를 모두 지난 건만 나갑니다.",
        "Stage 3 — 평상시 Autopilot. 사람 Feedback이 쌓여 보정 신뢰도가 HIGH가 된 뒤.",
        "※ 이 Phase는 LIVE를 켜지 않습니다. 전환은 운영자가 직접 결정합니다.",
    ]



def build_readiness(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: Optional[datetime] = None,
) -> ReadinessReport:
    """모든 점검을 모아 최종 판정을 만든다. **쓰기도 LLM 호출도 없다.**"""
    now = now or datetime.now(timezone.utc)
    report = ReadinessReport(generated_at=now.isoformat(timespec="seconds"))

    baseline = collect_baseline(conn, now=now)
    guards = collect_guards(config, conn)
    audit = collect_audit(conn)
    calibration = collect_calibration(conn, config)
    comments = collect_comment_calibration(conn, config)
    projection = collect_projection(conn, config, now=now)
    chain = collect_chain(conn)

    health_rows, health_warnings = _health_rows(baseline, calibration, comments, config)

    mode = effective_mode(config)
    report.config_state = {
        "autopilot.enabled": str(bool(config.get("autopilot.enabled", False))).lower(),
        "browser_executor.mode": str(config.get("browser_executor.mode", "")),
        "actions.execution.dry_run": str(bool(config.get("actions.execution.dry_run", True))).lower(),
        "scheduler.autopilot": str(bool(config.get("scheduler.autopilot", False))).lower(),
        "Effective": mode.as_line(),
    }

    broken = [link for link in chain if not link.complete]
    report.sections = {
        "운영 기준선": baseline.as_sections()["후보 모집단 (전체 기간)"],
        "Discovery / AI / 댓글 건강": health_rows,
        "Target 보정": calibration.as_rows(),
        "댓글 품질": comments.as_rows(),
        "Action 추정": projection.as_rows(),
        "안전장치": guards.as_rows(),
        "감사 추적": {
            **audit.as_rows(),
            "후보→실행 사슬": f"{len(chain)}건 검사 · 끊김 {len(broken)}건",
        },
        "설정 상태": report.config_state,
    }

    _classify(
        report,
        guards=guards,
        audit=audit,
        calibration=calibration,
        comments=comments,
        projection=projection,
        health_warnings=health_warnings,
    )
    if broken:
        report.blockers.extend(
            f"사슬 끊김: action {link.action_id} — {', '.join(link.broken)}" for link in broken
        )
        report.technical = NOT_READY
        report.verdict = NO_GO

    report.risks.extend(baseline.notes)
    report.risks.extend(projection.notes)
    report.recommendations.extend(calibration.findings)
    # 같은 사실이 여러 점검에서 나올 수 있다. 같은 말을 두 번 적으면
    # 문제가 두 개인 것처럼 읽힌다 — 순서는 지키면서 한 번만 남긴다.
    report.risks = _unique(report.risks)
    report.recommendations = _unique(report.recommendations)
    return report


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def render_readiness_html(report: ReadinessReport) -> str:
    """보고서를 HTML로 만든다(모든 값 escape)."""

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
<title>DailyReels Targeting Agent — Live Readiness</title>
<style>{STYLE}</style></head><body>
<h1>DailyReels Targeting Agent — Live Readiness</h1>
<p class="muted">{_e(report.generated_at)} · DB·설정만 읽어 생성(LLM 호출 없음)</p>

<div class="verdict {report.verdict_class}">{_e(report.verdict)}</div>
<table>
<tr><th>기술 준비도</th><td>{_e(report.technical)}</td></tr>
<tr><th>Target 보정 신뢰도</th><td>{_e(report.calibration_confidence)}</td></tr>
</table>

<h2>차단 사유</h2>
{bullets(report.blockers, "없음 — 안전장치와 감사 추적에 문제가 없습니다.")}

<h2>위험 요소</h2>
{bullets(report.risks, "특별한 위험 요소 없음")}

{sections}

<h2>권고</h2>
{bullets(report.recommendations, "권고 없음")}

<h2>단계적 전환(권고)</h2>
{bullets(report.rollout, "")}

<p class="muted">이 보고서는 LIVE를 켜지 않습니다. 설정은 운영자가 직접 바꿀 때만 변경됩니다.</p>
</body></html>
"""


def write_readiness_report(config: Config, report: ReadinessReport, html_text: str) -> Path:
    """보고서 파일과 live_readiness_latest.html을 저장한다."""
    output_dir = config._resolve_path(config.get("scheduler.summary.output_dir", "data/reports"))
    output_dir.mkdir(parents=True, exist_ok=True)
    date = report.generated_at[:10].replace("-", "") or "unknown"
    path = output_dir / f"{REPORT_PREFIX}{date}.html"
    path.write_text(html_text, encoding="utf-8")
    try:
        (output_dir / LATEST_NAME).write_text(html_text, encoding="utf-8")
    except OSError:  # pragma: no cover - 파일 잠김 등
        logger.warning("live_readiness_latest.html 생성 실패: %s", output_dir / LATEST_NAME)
    logger.info("Live Readiness Report 생성: %s", path)
    return path


def format_readiness(report: ReadinessReport) -> str:
    """터미널 요약."""
    lines = [
        "=" * 56,
        " DailyReels Targeting Agent — Live Readiness",
        "=" * 56,
        f" 판정          : {report.verdict}",
        f" 기술 준비도   : {report.technical}",
        f" 보정 신뢰도   : {report.calibration_confidence}",
    ]
    if report.blockers:
        lines.append(" 차단 사유     :")
        lines.extend(f"   - {item}" for item in report.blockers)
    if report.risks:
        lines.append(" 위험 요소     :")
        lines.extend(f"   - {item}" for item in report.risks)
    lines.append("=" * 56)
    return "\n".join(lines)
