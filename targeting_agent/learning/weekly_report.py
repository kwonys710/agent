"""Weekly Optimization Report (Phase 18C.1).

**LLM을 호출하지 않는다.** DB만 읽어 결정론적으로 만든다 — 같은 DB면 같은 보고서가 나온다.

담는 것: 최근 7일 Discovery/추출/Action/AI/검토 품질 + 추천 문구.
추천은 문장으로만 남기고, 임계값이나 Profile을 자동으로 바꾸지 않는다.
"""
from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.config import Config
from ..core.logger import get_logger
from .metrics import collect_health_metrics, collect_quality_metrics, recommend_thresholds
from .operations import collect_operations_report

logger = get_logger("learning.weekly")

REPORT_PREFIX = "weekly_report_"
LATEST_NAME = "weekly_latest.html"

STYLE = """
body{font-family:system-ui,"Malgun Gothic",sans-serif;margin:0;padding:24px;color:#1b1b1b;background:#fafafa}
h1{font-size:20px;margin:0 0 2px} h2{font-size:14px;margin:22px 0 8px;color:#333}
h3{font-size:13px;margin:14px 0 4px;color:#444}
.muted{color:#666;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff;margin-top:4px}
th,td{border:1px solid #e3e3e3;padding:5px 8px;text-align:left} th{background:#f5f5f5;width:240px}
ul{margin:6px 0 0 18px;font-size:13px}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


@dataclass
class WeeklyReport:
    """주간 보고서 데이터."""

    start_date: str
    end_date: str
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


def build_weekly_report(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: Optional[datetime] = None,
    days: int = 7,
) -> WeeklyReport:
    """최근 N일(기본 7일) 운영 보고서를 만든다."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).date().isoformat()
    end = now.date().isoformat()

    operations = collect_operations_report(conn, config, since=start)
    quality = collect_quality_metrics(conn)
    health = collect_health_metrics(conn, config)

    report = WeeklyReport(start_date=start, end_date=end)
    report.sections = {
        "검토 품질": quality.as_rows(),
        "AI / 운영 상태": health.as_rows(),
        **operations.as_sections(),
    }
    report.notes = list(health.warnings) + list(operations.notes)
    report.recommendations = recommend_thresholds(config, quality)
    return report


def render_weekly_html(report: WeeklyReport) -> str:
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
        f"<h3>{_e(title)}</h3>{rows(section)}" for title, section in report.sections.items()
    )
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>DailyReels Targeting Agent — Weekly Report {_e(report.end_date)}</title>
<style>{STYLE}</style></head><body>
<h1>DailyReels Targeting Agent</h1>
<p class="muted">Weekly Optimization Report · {_e(report.start_date)} ~ {_e(report.end_date)}
 · DB 기준 자동 생성(LLM 호출 없음)</p>

<h2>지표</h2>
{sections}

<h2>경고</h2>
{bullets(report.notes, "특별한 경고 없음")}

<h2>추천</h2>
{bullets(report.recommendations, "조정 추천 없음")}
<p class="muted">추천은 참고 정보다. 임계값과 Profile은 운영자가 명시적으로 바꿀 때만 변경된다
(--learn / --learn-apply).</p>
</body></html>
"""


def write_weekly_report(config: Config, report: WeeklyReport, html_text: str) -> Path:
    """보고서 파일과 weekly_latest.html을 저장한다."""
    output_dir = config._resolve_path(config.get("scheduler.summary.output_dir", "data/reports"))
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{REPORT_PREFIX}{report.end_date.replace('-', '')}.html"
    path.write_text(html_text, encoding="utf-8")
    try:
        (output_dir / LATEST_NAME).write_text(html_text, encoding="utf-8")
    except OSError:  # pragma: no cover - 파일 잠김 등
        logger.warning("weekly_latest.html 생성 실패: %s", output_dir / LATEST_NAME)
    logger.info("Weekly Report 생성: %s", path)
    return path
