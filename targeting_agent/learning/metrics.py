"""품질·운영 지표 (Phase 17).

DB query만으로 계산한다. Claude를 호출하지 않는다.
지표가 나쁘다고 설정을 자동으로 바꾸지 않는다 — 보고와 경고만 한다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..core.config import Config

SCORE_BANDS = ((90, 101, "90+"), (80, 90, "80-89"), (70, 80, "70-79"), (0, 70, "70 미만"))


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


@dataclass
class QualityMetrics:
    """검토 품질 지표."""

    reviewed: int = 0
    approved: int = 0
    approval_rate: Optional[float] = None
    good_target_rate: Optional[float] = None
    not_my_style_rate: Optional[float] = None
    claude_approval_rate: Optional[float] = None
    heuristic_approval_rate: Optional[float] = None
    comment_edit_rate: Optional[float] = None
    score_bands: dict[str, dict[str, object]] = field(default_factory=dict)

    def as_rows(self) -> dict[str, str]:
        rows = {
            "검토한 후보": str(self.reviewed),
            "승인": f"{self.approved} ({_pct(self.approval_rate)})",
            "GOOD_TARGET 비율": _pct(self.good_target_rate),
            "NOT_MY_STYLE 비율": _pct(self.not_my_style_rate),
            "Claude 분석 승인율": _pct(self.claude_approval_rate),
            "Heuristic 분석 승인율": _pct(self.heuristic_approval_rate),
            "댓글 수정률": _pct(self.comment_edit_rate),
        }
        for label, band in self.score_bands.items():
            rows[f"Score {label} 승인율"] = f"{_pct(band.get('rate'))} ({band.get('approved')}/{band.get('total')})"
        return rows


@dataclass
class HealthMetrics:
    """운영 상태 지표."""

    claude_calls: int = 0
    cache_hits: int = 0
    fallbacks: int = 0
    cache_hit_rate: Optional[float] = None
    fallback_rate: Optional[float] = None
    candidate_error_rate: Optional[float] = None
    scheduler_error_runs: int = 0
    scheduler_runs: int = 0
    duplicate_ingestion_rate: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    def as_rows(self) -> dict[str, str]:
        return {
            "Claude 호출 / Cache Hit": f"{self.claude_calls} / {self.cache_hits}",
            "Cache Hit 비율": _pct(self.cache_hit_rate),
            "Fallback 비율": _pct(self.fallback_rate),
            "후보 오류 비율": _pct(self.candidate_error_rate),
            "Scheduler 실패": f"{self.scheduler_error_runs} / {self.scheduler_runs}",
            "중복 입력 비율": _pct(self.duplicate_ingestion_rate),
        }


def collect_quality_metrics(conn: sqlite3.Connection) -> QualityMetrics:
    """승인/Feedback 기준 품질 지표."""

    def scalar(sql: str, *params: object) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    approved_media = (
        "SELECT DISTINCT media_pk FROM action_queue WHERE approved_at IS NOT NULL"
    )
    reviewed = scalar(
        "SELECT COUNT(*) FROM candidate_media WHERE status IN ('QUEUED','SKIPPED','INTERACTED')"
    )
    approved = scalar(f"SELECT COUNT(*) FROM ({approved_media})")
    metrics = QualityMetrics(
        reviewed=reviewed, approved=approved, approval_rate=_ratio(approved, reviewed)
    )

    feedback_total = scalar("SELECT COUNT(*) FROM feedback WHERE media_pk IS NOT NULL")
    metrics.good_target_rate = _ratio(
        scalar("SELECT COUNT(*) FROM feedback WHERE feedback_type = 'GOOD_TARGET'"), feedback_total
    )
    metrics.not_my_style_rate = _ratio(
        scalar("SELECT COUNT(*) FROM feedback WHERE feedback_type = 'NOT_MY_STYLE'"), feedback_total
    )

    # 후보별 최종 분석 방식으로 나눈다(한 후보가 양쪽 분모에 들어가지 않게).
    final_method = (
        "SELECT media_pk, CASE WHEN SUM(analyzer = 'claude_code') > 0 "
        "THEN 'claude_code' ELSE 'heuristic' END AS final_analyzer "
        "FROM media_analysis GROUP BY media_pk"
    )
    for analyzer, attribute in (
        ("claude_code", "claude_approval_rate"),
        ("heuristic", "heuristic_approval_rate"),
    ):
        total = scalar(f"SELECT COUNT(*) FROM ({final_method}) WHERE final_analyzer = ?", analyzer)
        approved_count = scalar(
            f"SELECT COUNT(*) FROM ({final_method}) WHERE final_analyzer = ? "
            f"AND media_pk IN ({approved_media})",
            analyzer,
        )
        setattr(metrics, attribute, _ratio(approved_count, total))

    selected = scalar("SELECT COUNT(*) FROM comment_drafts WHERE status = 'SELECTED'")
    edited = scalar(
        "SELECT COUNT(*) FROM comment_drafts WHERE status = 'SELECTED' AND generator = 'operator'"
    )
    metrics.comment_edit_rate = _ratio(edited, selected)

    for low, high, label in SCORE_BANDS:
        total = scalar(
            "SELECT COUNT(*) FROM candidate_media WHERE COALESCE(target_score, 0) >= ? "
            "AND COALESCE(target_score, 0) < ?",
            low,
            high,
        )
        approved_count = scalar(
            "SELECT COUNT(*) FROM candidate_media WHERE COALESCE(target_score, 0) >= ? "
            f"AND COALESCE(target_score, 0) < ? AND media_pk IN ({approved_media})",
            low,
            high,
        )
        metrics.score_bands[label] = {
            "total": total,
            "approved": approved_count,
            "rate": _ratio(approved_count, total),
        }
    return metrics


def collect_health_metrics(conn: sqlite3.Connection, config: Config) -> HealthMetrics:
    """AI/Scheduler 운영 상태와 경고."""

    def scalar(sql: str, *params: object) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    totals = {
        row["metric"]: int(row["total"])
        for row in conn.execute("SELECT metric, SUM(value) AS total FROM daily_stats GROUP BY metric")
    }
    health = HealthMetrics(
        claude_calls=totals.get("claude_requests", 0),
        cache_hits=totals.get("claude_cache_hits", 0),
        fallbacks=totals.get("claude_fallbacks", 0),
    )
    attempts = health.claude_calls + health.cache_hits
    health.cache_hit_rate = _ratio(health.cache_hits, attempts)
    health.fallback_rate = _ratio(health.fallbacks, max(attempts, health.fallbacks))

    candidates = scalar("SELECT COUNT(*) FROM candidate_media")
    health.candidate_error_rate = _ratio(
        scalar("SELECT COUNT(*) FROM candidate_media WHERE status = 'ERROR'"), candidates
    )
    health.scheduler_runs = scalar("SELECT COUNT(*) FROM scheduled_runs")
    health.scheduler_error_runs = scalar(
        "SELECT COUNT(*) FROM scheduled_runs WHERE status = 'FAILED' OR errors > 0"
    )
    imports = scalar("SELECT COUNT(*) FROM import_events")
    health.duplicate_ingestion_rate = _ratio(
        scalar("SELECT COUNT(*) FROM import_events WHERE result = 'DUPLICATE'"), imports
    )

    min_samples = int(config.get("learning.health.minimum_samples", 10))
    if attempts >= min_samples:
        if health.fallback_rate is not None and health.fallback_rate >= float(
            config.get("learning.health.fallback_rate_warning", 0.30)
        ):
            health.warnings.append(f"Fallback 비율이 높습니다({_pct(health.fallback_rate)}).")
        if health.cache_hit_rate is not None and health.cache_hit_rate <= float(
            config.get("learning.health.cache_hit_minimum_warning", 0.20)
        ):
            health.warnings.append(f"Cache Hit 비율이 낮습니다({_pct(health.cache_hit_rate)}).")
    if candidates >= min_samples and health.candidate_error_rate is not None:
        if health.candidate_error_rate >= float(
            config.get("learning.health.candidate_error_warning", 0.10)
        ):
            health.warnings.append(f"후보 오류 비율이 높습니다({_pct(health.candidate_error_rate)}).")
    return health


def recommend_thresholds(config: Config, metrics: QualityMetrics) -> list[str]:
    """임계값 추천만 만든다. **설정을 자동으로 바꾸지 않는다.**"""
    recommendations: list[str] = []
    minimum = float(config.get("scoring.minimum_target_score", 70))
    comment_threshold = float(config.get("actions.require_score_for_comment", 82))
    min_band = int(config.get("learning.threshold.minimum_band_samples", 5))

    for label, band in metrics.score_bands.items():
        total = int(band.get("total") or 0)
        rate = band.get("rate")
        if total < min_band or rate is None:
            continue
        if rate <= 0.2 and label in ("70-79", "80-89"):
            recommendations.append(
                f"Score {label} 구간 승인율이 {_pct(rate)}입니다 — "
                f"minimum_target_score({minimum:.0f}) 또는 "
                f"require_score_for_comment({comment_threshold:.0f}) 상향을 검토하세요."
            )
        if rate >= 0.8 and label == "70-79":
            recommendations.append(
                f"Score {label} 구간 승인율이 {_pct(rate)}로 높습니다 — 기준 하향을 검토할 수 있습니다."
            )
    if not recommendations:
        recommendations.append("임계값을 조정할 만한 뚜렷한 신호가 없습니다(표본 부족 포함).")
    recommendations.append("※ 추천일 뿐이며 config는 자동으로 변경되지 않습니다.")
    return recommendations
