"""Daily Summary HTML 생성 (Phase 15).

DB만 읽어 결정론적으로 만든다. **Summary 문장 생성을 위해 Claude를 호출하지 않는다.**
사용자 입력(캡션/username/메모)은 모두 HTML escape 후 출력한다.
"""
from __future__ import annotations

import html
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.config import Config
from ..core.logger import get_logger

logger = get_logger("scheduler.summary")

REPORT_PREFIX = "daily_summary_"
LATEST_NAME = "latest.html"

STYLE = """
body{font-family:system-ui,"Malgun Gothic",sans-serif;margin:0;padding:24px;color:#1b1b1b;background:#fafafa}
h1{font-size:20px;margin:0 0 2px} h2{font-size:14px;margin:22px 0 8px;color:#333}
.muted{color:#666;font-size:12px}
.grid{display:flex;flex-wrap:wrap;gap:10px}
.stat{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:8px 12px;min-width:112px}
.stat b{display:block;font-size:18px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff;margin-top:4px}
th,td{border:1px solid #e3e3e3;padding:5px 8px;text-align:left} th{background:#f5f5f5;width:220px}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


@dataclass
class SummaryData:
    """Summary에 들어갈 집계값."""

    date: str
    candidate: dict[str, int] = field(default_factory=dict)
    ai: dict[str, int] = field(default_factory=dict)
    score: dict[str, int] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)
    feedback: dict[str, int] = field(default_factory=dict)
    inbox: dict[str, Any] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)
    learning: dict[str, Any] = field(default_factory=dict)


def _local_date(column: str, tz_offset: int) -> str:
    return f"date({column}, '{tz_offset:+d} hours')"


def build_summary_data(
    conn: sqlite3.Connection,
    config: Config,
    date: str,
    *,
    inbox: Optional[Mapping[str, Any]] = None,
    run: Optional[Mapping[str, Any]] = None,
) -> SummaryData:
    """오늘자 집계를 DB에서 만든다."""
    tz = int(config.get("actions.daily_limits.timezone_offset_hours", 9))

    def scalar(sql: str, *params: Any) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    statuses = {
        row["status"]: int(row["n"])
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM candidate_media GROUP BY status")
    }
    stats = {
        row["metric"]: int(row["value"])
        for row in conn.execute(
            "SELECT metric, value FROM daily_stats WHERE stat_date = ?", (date,)
        )
    }
    queue = {
        f"{row['status']}:{int(row['approved'] or 0)}": int(row["n"])
        for row in conn.execute(
            "SELECT status, (approved_at IS NOT NULL) AS approved, COUNT(*) AS n "
            "FROM action_queue GROUP BY 1, 2"
        )
    }

    candidate = {
        "registered_today": scalar(
            f"SELECT COUNT(*) FROM candidate_media WHERE {_local_date('discovered_at', tz)} = ?", date
        ),
        "analyzed_today": scalar(
            f"SELECT COUNT(DISTINCT media_pk) FROM media_analysis WHERE {_local_date('analyzed_at', tz)} = ?",
            date,
        ),
        "needs_enrichment": statuses.get("NEEDS_ENRICHMENT", 0),
        "skipped": statuses.get("SKIPPED", 0),
        "review_pending": statuses.get("SCORED", 0) + statuses.get("NEW", 0),
        "total": sum(statuses.values()),
    }
    ai = {
        "claude_calls": stats.get("claude_requests", 0),
        "claude_limit": int(config.get("ai.daily_request_limit", 0)),
        "cache_hits": stats.get("claude_cache_hits", 0),
        "fallbacks": stats.get("claude_fallbacks", 0),
        "claude_analyzed": scalar(
            "SELECT COUNT(*) FROM media_analysis WHERE analyzer = 'claude_code'"
        ),
        "heuristic_analyzed": scalar(
            "SELECT COUNT(*) FROM media_analysis WHERE analyzer = 'heuristic'"
        ),
    }
    minimum = int(float(config.get("scoring.minimum_target_score", 70)))
    auto = int(float(config.get("scoring.auto_action_score", 82)))
    score = {
        f"above_{minimum}": scalar(
            "SELECT COUNT(*) FROM candidate_media WHERE COALESCE(target_score, 0) >= ?", minimum
        ),
        f"above_{auto}": scalar(
            "SELECT COUNT(*) FROM candidate_media WHERE COALESCE(target_score, 0) >= ?", auto
        ),
    }
    actions = {
        "awaiting_approval": queue.get("PENDING:0", 0),
        "approved_waiting": queue.get("PENDING:1", 0),
        "success": queue.get("SUCCESS:0", 0) + queue.get("SUCCESS:1", 0),
        "failed": queue.get("FAILED:0", 0) + queue.get("FAILED:1", 0),
    }
    feedback = {
        row["feedback_type"]: int(row["n"])
        for row in conn.execute(
            "SELECT feedback_type, COUNT(*) AS n FROM feedback GROUP BY feedback_type"
        )
    }
    from ..dashboard.queries import learning_overview

    overview = learning_overview(conn)
    learning = {
        "Active Profile": overview["version"],
        "Feedback 표본": overview["feedback_count"],
        "마지막 학습": (overview["last_run_at"] or "-")[:19],
        "상태": overview["last_status"] or "-",
        "최근 변경": " / ".join(overview["changes"][:5]) or "변경 없음",
    }

    return SummaryData(
        date=date,
        learning=learning,
        candidate=candidate,
        ai=ai,
        score=score,
        actions=actions,
        feedback=feedback,
        inbox=dict(inbox or {}),
        run=dict(run or {}),
    )


def render_summary_html(data: SummaryData) -> str:
    """집계값을 HTML로 만든다(모든 값은 escape 처리)."""

    def tiles(items: list[tuple[str, Any]]) -> str:
        return '<div class="grid">' + "".join(
            f'<div class="stat">{_e(label)}<b>{_e(value)}</b></div>' for label, value in items
        ) + "</div>"

    def rows(mapping: Mapping[str, Any]) -> str:
        if not mapping:
            return '<p class="muted">없음</p>'
        body = "".join(
            f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>" for key, value in mapping.items()
        )
        return f"<table>{body}</table>"

    score_items = [(key.replace("above_", "Score >= "), value) for key, value in data.score.items()]
    inbox = data.inbox or {}
    run = data.run or {}

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>DailyReels Targeting Agent — Daily Summary {_e(data.date)}</title>
<style>{STYLE}</style></head><body>
<h1>DailyReels Targeting Agent</h1>
<p class="muted">Daily Summary · {_e(data.date)} · 생성 {_e(run.get("finished_at", ""))}</p>

<h2>Candidate</h2>
{tiles([
    ("오늘 등록", data.candidate.get("registered_today", 0)),
    ("오늘 분석", data.candidate.get("analyzed_today", 0)),
    ("정보 부족", data.candidate.get("needs_enrichment", 0)),
    ("Skip", data.candidate.get("skipped", 0)),
    ("Review 대기", data.candidate.get("review_pending", 0)),
    ("전체 후보", data.candidate.get("total", 0)),
])}

<h2>AI (Claude Code)</h2>
{tiles([
    ("오늘 호출", f'{data.ai.get("claude_calls", 0)} / {data.ai.get("claude_limit", 0)}'),
    ("Cache Hit", data.ai.get("cache_hits", 0)),
    ("Fallback", data.ai.get("fallbacks", 0)),
    ("Claude 분석", data.ai.get("claude_analyzed", 0)),
    ("Heuristic 분석", data.ai.get("heuristic_analyzed", 0)),
])}

<h2>Score</h2>
{tiles(score_items)}

<h2>Action Queue</h2>
{tiles([
    ("승인 대기", data.actions.get("awaiting_approval", 0)),
    ("실행 대기(승인됨)", data.actions.get("approved_waiting", 0)),
    ("SUCCESS", data.actions.get("success", 0)),
    ("FAILED", data.actions.get("failed", 0)),
])}
<p class="muted">Scheduled Run은 Action을 실행하지 않는다. 실행은 운영자가 별도로 진행한다.</p>

<h2>Feedback</h2>
{rows(data.feedback)}

<h2>Inbox</h2>
{rows(inbox)}

<h2>Learning</h2>
{rows(data.learning)}
<p class="muted">학습은 자동 실행되지 않는다. 필요할 때 --learn 으로 확인한 뒤 --learn-apply 로 적용한다.</p>

<h2>이번 실행</h2>
{rows(run)}
</body></html>
"""


def write_summary(config: Config, data: SummaryData, html_text: str) -> Path:
    """리포트 파일과 latest.html을 저장한다."""
    output_dir = config._resolve_path(
        config.get("scheduler.summary.output_dir", "data/reports")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{REPORT_PREFIX}{data.date.replace('-', '')}.html"
    path.write_text(html_text, encoding="utf-8")

    # Windows 권한 문제를 피하려고 symlink 대신 복사본을 둔다.
    try:
        shutil.copyfile(path, output_dir / LATEST_NAME)
    except OSError:  # pragma: no cover - 파일 잠김 등
        logger.warning("latest.html 생성 실패: %s", output_dir / LATEST_NAME)
    return path


def cleanup_old_reports(config: Config, now: Optional[datetime] = None) -> int:
    """보관 기간이 지난 daily_summary_*.html만 정리한다(다른 파일은 건드리지 않는다)."""
    keep_days = int(config.get("scheduler.summary.keep_days", 30))
    if keep_days <= 0:
        return 0
    output_dir = config._resolve_path(
        config.get("scheduler.summary.output_dir", "data/reports")
    )
    if not output_dir.exists():
        return 0

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    removed = 0
    for path in output_dir.glob(f"{REPORT_PREFIX}*.html"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff:
                path.unlink()
                removed += 1
        except OSError:  # pragma: no cover
            continue
    return removed
