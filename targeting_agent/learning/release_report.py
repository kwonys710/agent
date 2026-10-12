"""v0.3 Release Readiness Report (Phase 19L).

"지금 릴리스해도 되는가"를 **DB만 읽어** 한 장으로 정리한다.

지키는 것:
- **LLM/Claude를 호출하지 않는다.** 같은 DB면 항상 같은 보고서가 나온다(결정론적).
- **DB에 쓰지 않는다.** 집계 조회만 한다(상태를 바꾸지 않는다).
- **임계값을 바꾸지 않는다.** config 값을 읽어 표시만 한다.
- 표본이 없는 칸은 숫자를 만들어 내지 않고 `INSUFFICIENT_DATA`로 남긴다.
- 모든 값은 `html.escape`를 거친다.
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

logger = get_logger("learning.release_report")

REPORT_PREFIX = "v0_3_release_readiness_"
LATEST_NAME = "v0_3_release_readiness_latest.html"

# 표본이 없을 때 쓰는 표시. 0%나 0.0으로 단정하지 않는다.
NO_DATA = "INSUFFICIENT_DATA"

SECTION_ORDER = (
    "Query Bank",
    "Query Diversity",
    "Discovery Novelty",
    "Freshness",
    "Target Score",
    "LIKE Readiness",
    "Comment Readiness",
    "Activity Health",
    "Safety",
    "Regression",
)
GATES_SECTION = "Remaining Operator Gates"

STYLE = """
body{font-family:system-ui,"Malgun Gothic",sans-serif;margin:0;padding:24px;color:#1b1b1b;background:#fafafa}
h1{font-size:20px;margin:0 0 2px} h2{font-size:14px;margin:22px 0 8px;color:#333}
h3{font-size:13px;margin:16px 0 4px;color:#444}
.muted{color:#666;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff;margin-top:4px}
th,td{border:1px solid #e3e3e3;padding:5px 8px;text-align:left} th{background:#f5f5f5;width:260px}
ul{margin:6px 0 0 18px;font-size:13px}
code{background:#f1f1f1;padding:1px 4px;border-radius:3px}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _pct(value: Optional[float]) -> str:
    """비율 표시. 표본이 없으면 INSUFFICIENT_DATA."""
    return NO_DATA if value is None else f"{value * 100:.0f}%"


def _num(value: Optional[float], digits: int = 1) -> str:
    return NO_DATA if value is None else f"{float(value):.{digits}f}"


def _count(value: Optional[int]) -> str:
    """개수는 0도 의미가 있다 — 집계 자체가 없을 때만 INSUFFICIENT_DATA."""
    return NO_DATA if value is None else str(int(value))


def _scalar(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    return int((row[0] if row else 0) or 0)


@dataclass
class ReleaseReadiness:
    """릴리스 준비 상태 데이터(표 섹션 + 남은 운영자 게이트)."""

    generated_at: str
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    gates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_release_readiness(
    conn: sqlite3.Connection, config: Config, *, now: Optional[datetime] = None
) -> ReleaseReadiness:
    """v0.3 릴리스 준비 상태를 모은다(DB 읽기 전용 · LLM 호출 없음)."""
    from ..dashboard import queries as dashboard_queries
    from .operations import (
        collect_executor_metrics,
        collect_freshness_metrics,
        collect_novelty_metrics,
        collect_operations_report,
        collect_query_metrics,
        query_category_distribution,
    )

    now = now or datetime.now(timezone.utc)
    query = collect_query_metrics(conn)
    novelty = collect_novelty_metrics(conn)
    freshness = collect_freshness_metrics(conn, now=now)
    executor = collect_executor_metrics(conn)
    operations = collect_operations_report(conn, config)
    engagement = dashboard_queries.engagement_overview(conn, config)
    categories = query_category_distribution(conn)

    readiness = ReleaseReadiness(generated_at=now.isoformat(timespec="seconds"))
    readiness.sections = {
        "Query Bank": _query_bank_section(query),
        "Query Diversity": _diversity_section(query, novelty, categories),
        "Discovery Novelty": _novelty_section(novelty),
        "Freshness": _freshness_section(freshness),
        "Target Score": _target_score_section(conn, operations),
        "LIKE Readiness": _like_section(engagement),
        "Comment Readiness": _comment_section(engagement),
        "Activity Health": _activity_section(operations, executor),
        "Safety": _safety_section(conn, config, executor),
        "Regression": _regression_section(conn),
    }
    readiness.gates = _operator_gates(conn, config, engagement, executor)
    readiness.notes = list(operations.notes)
    return readiness


# --- 섹션 ------------------------------------------------------------------
def _query_bank_section(query: Any) -> dict[str, str]:
    status = query.status or {}
    return {
        "검색어 은행 크기": _count(query.bank_size),
        "ACTIVE / EXPERIMENTAL": f"{status.get('ACTIVE', 0)} / {status.get('EXPERIMENTAL', 0)}",
        "COOLDOWN / DISABLED": f"{status.get('COOLDOWN', 0)} / {status.get('DISABLED', 0)}",
        "한 번도 쓰지 않은 검색어": _count(query.never_run),
        "평균 Utility": _num(query.avg_utility, 2),
        "마지막 선택 묶음": ", ".join(query.last_batch) or NO_DATA,
    }


def _diversity_section(query: Any, novelty: Any, categories: Mapping[str, int]) -> dict[str, str]:
    total_runs = sum(categories.values())
    top = next(iter(categories.items()), None)
    return {
        "카테고리 수(은행)": _count(query.categories),
        "실제 사용된 검색어": _count(query.queries_run),
        "실행된 카테고리": _count(len(categories)) if categories else NO_DATA,
        "최다 사용 카테고리": (
            f"{top[0]} {top[1]}회 ({top[1] / total_runs * 100:.0f}%)" if top and total_runs else NO_DATA
        ),
        "카테고리별 실행": ", ".join(f"{k} {v}" for k, v in categories.items()) or NO_DATA,
        "여러 검색어에서 발견된 후보": _count(novelty.multi_query_candidates),
    }


def _novelty_section(novelty: Any) -> dict[str, str]:
    return {
        "후보 수": _count(novelty.candidates),
        "반복 발견 후보": _count(novelty.repeat_candidates),
        "반복 발견 비율": _pct(novelty.repeat_rate),
        "최대 발견 횟수": _count(novelty.max_seen_count) if novelty.candidates else NO_DATA,
        "반복 발견 처리": "metadata만 갱신 — 재분석/재Action 없음(19D)",
    }


def _freshness_section(freshness: Any) -> dict[str, str]:
    rows: dict[str, str] = {}
    for label, count in freshness.bands.items():
        rows[f"{label} 후보"] = _count(count)
    rows["7일 이내 비율"] = _pct(freshness.fresh_rate)
    rows["비고"] = "처리 우선순위일 뿐이며 Target Score를 깎지 않는다(19E)"
    return rows


def _target_score_section(conn: sqlite3.Connection, operations: Any) -> dict[str, str]:
    scored = _scalar(conn, "SELECT COUNT(*) FROM candidate_media WHERE target_score IS NOT NULL")
    row = conn.execute(
        "SELECT AVG(target_score) AS avg, MAX(target_score) AS max FROM candidate_media "
        "WHERE target_score IS NOT NULL"
    ).fetchone()
    rows = {
        "점수가 있는 후보": _count(scored),
        "평균 Target Score": _num(row["avg"] if scored else None),
        "최고 Target Score": _num(row["max"] if scored else None, 0),
    }
    for band, count in operations.score_distribution.items():
        rows[f"Score {band}"] = _count(count) if scored else NO_DATA
    recommendation = operations.action_recommendation or {}
    rows["추천 분포(없음/LIKE/LIKE+COMMENT)"] = (
        f"{recommendation.get('none', 0)} / {recommendation.get('like', 0)} / "
        f"{recommendation.get('like_comment', 0)}"
        if scored
        else NO_DATA
    )
    return rows


def _like_section(engagement: Mapping[str, Any]) -> dict[str, str]:
    like = engagement["like"]
    thresholds = engagement["thresholds"]
    executed = int(like["executed"])
    confirmed_rate = (int(like["confirmed"]) / executed) if executed else None
    return {
        "추천 임계값": f"Score {thresholds['like']}+ (config 값 그대로)",
        "임계값 충족 후보": _count(like["eligible"]),
        "실행(Dry Run 포함)": _count(executed),
        "실제 수행 확인": _count(like["confirmed"]) if executed else NO_DATA,
        "확인 비율": _pct(confirmed_rate),
        "실패 / 보류": _count(like["blocked"]),
    }


def _comment_section(engagement: Mapping[str, Any]) -> dict[str, str]:
    comment = engagement["comment"]
    thresholds = engagement["thresholds"]
    drafts = int(comment["quality_pass"]) + int(comment["quality_reject"])
    quality_rate = (int(comment["quality_pass"]) / drafts) if drafts else None
    posted = int(comment["confirmed"]) + int(comment["unknown"])
    return {
        "추천 임계값": f"Score {thresholds['comment']}+ (config 값 그대로)",
        "임계값 충족 후보": _count(comment["eligible"]),
        "댓글 후보(통과/탈락)": (
            f"{comment['quality_pass']} / {comment['quality_reject']}" if drafts else NO_DATA
        ),
        "품질 통과율": _pct(quality_rate),
        "Dry Run 댓글": _count(comment["dry_run"]),
        "게시 확인됨": _count(comment["confirmed"]) if posted else NO_DATA,
        "확인 불가(보냈다고 단정하지 않음)": _count(comment["unknown"]) if posted else NO_DATA,
    }


def _activity_section(operations: Any, executor: Any) -> dict[str, str]:
    discovery = operations.discovery
    return {
        "Discovery 실행 / 실패": f"{discovery.runs} / {discovery.failed_runs}",
        "검색어 실패율": _pct(discovery.selector_failure_rate),
        "Username 추출률": _pct(discovery.username_rate),
        "Caption 추출률": _pct(discovery.caption_rate),
        "Action Queue": _count(executor.queued),
        "실행 성공률": _pct(executor.success_rate),
        "Autopilot 실행 / 중단": f"{executor.autopilot_runs} / {executor.autopilot_halted_runs}",
    }


def _safety_section(conn: sqlite3.Connection, config: Config, executor: Any) -> dict[str, str]:
    from ..actions.policy import RunMode

    executor_mode = str(config.get("browser_executor.mode", "DRY_RUN")).upper()
    blocked = _scalar(
        conn, "SELECT COUNT(*) FROM action_queue WHERE status IN ('FAILED', 'SKIPPED', 'CANCELLED')"
    )
    return {
        "운용 모드": RunMode.from_config(config).value,
        "Executor 모드": executor_mode,
        "config dry_run": str(bool(config.dry_run)),
        "실제 쓰기 / Dry Run": f"{executor.real_writes} / {executor.dry_run_writes}",
        "승인 대기 Action": _count(executor.awaiting_approval),
        "실패·보류 Action": _count(blocked),
        "Autopilot 중단": _count(executor.autopilot_halted_runs),
        "지원 Action": "LIKE / COMMENT 만 — FOLLOW·DM·저장·공유 없음",
    }


def _regression_section(conn: sqlite3.Connection) -> dict[str, str]:
    """릴리스 전에 눈으로 확인할 불변식(DB 조회만)."""
    candidates = _scalar(conn, "SELECT COUNT(*) FROM candidate_media")
    unscored = _scalar(conn, "SELECT COUNT(*) FROM candidate_media WHERE target_score IS NULL")
    unresolved = _scalar(conn, "SELECT COUNT(*) FROM creators WHERE username LIKE 'unresolved:%'")
    duplicate_actions = _scalar(
        conn,
        "SELECT COUNT(*) FROM (SELECT media_pk, action_type FROM action_queue "
        "GROUP BY media_pk, action_type HAVING COUNT(*) > 1)",
    )
    orphan_queries = _scalar(
        conn,
        "SELECT COUNT(*) FROM candidate_queries cq LEFT JOIN query_bank q "
        "ON q.query_id = cq.query_id WHERE q.query_id IS NULL",
    )
    failed_actions = _scalar(conn, "SELECT COUNT(*) FROM action_queue WHERE status = 'FAILED'")
    return {
        "후보 수": _count(candidates),
        "점수 없는 후보": _count(unscored) if candidates else NO_DATA,
        "미해결 Creator(unresolved:)": _count(unresolved),
        "중복 Action(media+type)": _count(duplicate_actions),
        "고아 candidate_queries": _count(orphan_queries),
        "실패 Action": _count(failed_actions),
        "보고서 생성 방식": "DB 조회만 — LLM 호출 없음 · DB 쓰기 없음",
    }


def _operator_gates(
    conn: sqlite3.Connection,
    config: Config,
    engagement: Mapping[str, Any],
    executor: Any,
) -> list[str]:
    """운영자가 아직 직접 해야 하는 일. Agent가 스스로 통과시키지 않는다."""
    from ..actions.policy import RunMode

    gates: list[str] = []
    executor_mode = str(config.get("browser_executor.mode", "DRY_RUN")).upper()
    if executor_mode != "LIVE" or config.dry_run:
        gates.append(
            f"Executor가 {executor_mode}입니다 — 실제 수행은 운영자가 LIVE로 바꿀 때만 시작됩니다."
        )
    if RunMode.from_config(config) is RunMode.REVIEW:
        gates.append("REVIEW 모드입니다 — Action 승인은 Dashboard에서 운영자가 직접 합니다.")
    if executor.awaiting_approval:
        gates.append(f"승인 대기 Action {executor.awaiting_approval}건을 검토해야 합니다.")
    if int(engagement["comment"]["unknown"]):
        gates.append(
            f"게시 확인이 안 된 댓글 {engagement['comment']['unknown']}건이 있습니다 — "
            "Instagram에서 직접 확인이 필요합니다."
        )
    if not _scalar(conn, "SELECT COUNT(*) FROM query_runs"):
        gates.append("검색어 실행 기록이 없습니다 — Query Bank 성과 표본을 먼저 모아야 합니다.")
    if not _scalar(conn, "SELECT COUNT(*) FROM feedback"):
        gates.append("Feedback이 없습니다 — 학습/임계값 조정 판단에는 운영자 Feedback이 필요합니다.")
    gates.append("임계값(LIKE/COMMENT)과 Profile 변경은 운영자 명령으로만 적용합니다.")
    return gates


# --- 렌더링 ----------------------------------------------------------------
def render_release_html(readiness: ReleaseReadiness) -> str:
    """릴리스 준비 보고서를 HTML로 만든다(모든 값 escape)."""

    def rows(mapping: Mapping[str, Any]) -> str:
        if not mapping:
            return f'<p class="muted">{_e(NO_DATA)}</p>'
        body = "".join(
            f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>" for key, value in mapping.items()
        )
        return f"<table>{body}</table>"

    def bullets(items: list[str], empty: str) -> str:
        if not items:
            return f'<p class="muted">{_e(empty)}</p>'
        return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in items) + "</ul>"

    sections = "".join(
        f"<h3>{_e(title)}</h3>{rows(readiness.sections.get(title, {}))}" for title in SECTION_ORDER
    )
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>DailyReels Targeting Agent — v0.3 Release Readiness</title>
<style>{STYLE}</style></head><body>
<h1>DailyReels Targeting Agent — v0.3 Release Readiness</h1>
<p class="muted">생성 {_e(readiness.generated_at)} · DB 기준 결정론적 생성(LLM 호출 없음 · DB 쓰기 없음)</p>
<p class="muted">표본이 없는 칸은 <code>{_e(NO_DATA)}</code>로 둡니다 — 숫자를 만들어 내지 않습니다.</p>

<h2>지표</h2>
{sections}

<h2>{_e(GATES_SECTION)}</h2>
{bullets(readiness.gates, "남은 게이트 없음")}

<h2>경고</h2>
{bullets(readiness.notes, "특별한 경고 없음")}
<p class="muted">이 보고서는 상태를 바꾸지 않습니다. 임계값·Profile·Executor 모드는
운영자가 명시적으로 바꿀 때만 변경됩니다.</p>
</body></html>
"""


def write_release_report(
    config: Config, readiness: ReleaseReadiness, html_text: str
) -> Path:
    """날짜별 파일과 `v0_3_release_readiness_latest.html`을 저장한다."""
    output_dir = config._resolve_path(config.get("scheduler.summary.output_dir", "data/reports"))
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = readiness.generated_at[:10].replace("-", "")
    path = output_dir / f"{REPORT_PREFIX}{stamp}.html"
    path.write_text(html_text, encoding="utf-8")
    try:
        (output_dir / LATEST_NAME).write_text(html_text, encoding="utf-8")
    except OSError:  # pragma: no cover - 파일 잠김 등
        logger.warning("%s 생성 실패: %s", LATEST_NAME, output_dir / LATEST_NAME)
    logger.info("Release Readiness Report 생성: %s", path)
    return path
