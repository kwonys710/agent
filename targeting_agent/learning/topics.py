"""Topic Weight 학습 (Phase 17) — 설명 가능한 결정론적 계산.

ML 프레임워크를 쓰지 않는다. Claude도 호출하지 않는다.
이미 DB에 저장된 Feedback / 승인 / 분석 topic 만으로 계산한다.

계산 흐름:
  1. Feedback·승인·Skip을 topic별 신호로 모은다(신호 강도는 config에서 관리).
  2. topic별 평균 신호 → 변화량(delta). 한 번에 max_delta 이상 움직이지 않는다.
  3. 결과 가중치는 min~max 범위로 자른다.
  4. 표본이 부족하면 아무 것도 바꾸지 않는다(NOT_ENOUGH_DATA).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Mapping, Optional

from ..core.config import Config
from ..core.logger import get_logger
from ..core.models import FeedbackType

logger = get_logger("learning.topics")

STATUS_OK = "OK"
STATUS_NOT_ENOUGH_DATA = "NOT_ENOUGH_DATA"
STATUS_NO_CHANGE = "NO_CHANGE"

# 신호 강도 기본값(config.learning.signals 로 덮어쓸 수 있다)
DEFAULT_SIGNALS: dict[str, float] = {
    FeedbackType.GOOD_TARGET.value: 1.0,     # 강한 positive
    FeedbackType.RESPONDED.value: 1.0,       # 강한 positive(상대 반응)
    FeedbackType.FOLLOWED.value: 1.0,        # 강한 positive
    FeedbackType.NOT_MY_STYLE.value: -1.2,   # 강한 negative
    FeedbackType.BAD_TARGET.value: -1.0,
    "APPROVED": 0.5,                          # 중간 positive(운영자가 승인함)
    "SKIPPED": -0.3,                          # 약한 negative(보류)
}


@dataclass
class TopicSignal:
    """한 topic에 모인 신호."""

    topic: str
    total: float = 0.0
    samples: int = 0
    sources: dict[str, int] = field(default_factory=dict)

    @property
    def average(self) -> float:
        return self.total / self.samples if self.samples else 0.0

    def add(self, source: str, value: float) -> None:
        self.total += value
        self.samples += 1
        self.sources[source] = self.sources.get(source, 0) + 1


@dataclass
class TopicChange:
    """topic 하나의 가중치 변경."""

    topic: str
    before: float
    after: float
    delta: float
    samples: int
    reason: str

    def as_line(self) -> str:
        return f"{self.topic} {self.before:.2f} → {self.after:.2f} ({self.delta:+.2f}, 표본 {self.samples})"


@dataclass
class LearningPlan:
    """학습 결과(적용 전)."""

    status: str = STATUS_OK
    feedback_count: int = 0
    changes: list[TopicChange] = field(default_factory=list)
    new_weights: dict[str, float] = field(default_factory=dict)
    signals: list[TopicSignal] = field(default_factory=list)
    message: str = ""

    @property
    def has_change(self) -> bool:
        return bool(self.changes)


def _topics_of(payload: Optional[str]) -> list[str]:
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    topics = [str(t).strip() for t in (data.get("topics") or []) if str(t).strip()]
    primary = str(data.get("primary_topic") or "").strip()
    if primary and primary not in topics:
        topics.insert(0, primary)
    return [t for t in topics if t != "기타"]


def _media_topics(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """후보별 topic 목록(Claude 분석 우선)."""
    rows = conn.execute(
        "SELECT media_pk, analyzer, payload FROM media_analysis "
        "ORDER BY (analyzer = 'claude_code') DESC, analyzed_at DESC"
    ).fetchall()
    topics: dict[int, list[str]] = {}
    for row in rows:
        media_pk = int(row["media_pk"])
        if media_pk in topics:
            continue
        found = _topics_of(row["payload"])
        if found:
            topics[media_pk] = found
    return topics


def collect_signals(
    conn: sqlite3.Connection, signal_weights: Mapping[str, float]
) -> tuple[dict[str, TopicSignal], int]:
    """Feedback / 승인 / Skip을 topic 신호로 모은다. (신호, feedback 건수)"""
    topics_by_media = _media_topics(conn)
    signals: dict[str, TopicSignal] = {}

    def add(media_pk: int, source: str) -> None:
        value = float(signal_weights.get(source, 0.0))
        if not value:
            return
        for topic in topics_by_media.get(media_pk, []):
            signals.setdefault(topic, TopicSignal(topic=topic)).add(source, value)

    feedback_rows = conn.execute(
        "SELECT media_pk, feedback_type FROM feedback WHERE media_pk IS NOT NULL"
    ).fetchall()
    for row in feedback_rows:
        add(int(row["media_pk"]), str(row["feedback_type"]))

    # 운영자가 승인한 Action = 중간 강도의 positive
    for row in conn.execute(
        "SELECT DISTINCT media_pk FROM action_queue WHERE approved_at IS NOT NULL"
    ):
        add(int(row["media_pk"]), "APPROVED")

    # 검토 후 보류(Skip) = 약한 negative
    for row in conn.execute(
        "SELECT media_pk FROM candidate_media WHERE status = 'SKIPPED'"
    ):
        add(int(row["media_pk"]), "SKIPPED")

    return signals, len(feedback_rows)


def plan_learning(conn: sqlite3.Connection, config: Config, current: Mapping[str, float]) -> LearningPlan:
    """현재 가중치에서 출발해 조정안을 만든다(DB는 바꾸지 않는다)."""
    signal_weights = {**DEFAULT_SIGNALS, **config.section("learning.signals")}
    min_feedback = int(config.get("learning.minimum_feedback_count", 10))
    min_samples = int(config.get("learning.minimum_topic_samples", 3))
    max_delta = float(config.get("learning.max_delta_per_learning_run", 0.05))
    min_weight = float(config.get("learning.min_topic_weight", 0.5))
    max_weight = float(config.get("learning.max_topic_weight", 1.5))

    signals, feedback_count = collect_signals(conn, signal_weights)
    plan = LearningPlan(
        feedback_count=feedback_count,
        new_weights=dict(current),
        signals=sorted(signals.values(), key=lambda s: s.average, reverse=True),
    )

    if feedback_count < min_feedback:
        plan.status = STATUS_NOT_ENOUGH_DATA
        plan.message = f"Feedback이 부족합니다({feedback_count}/{min_feedback}) — Profile을 변경하지 않습니다."
        return plan

    for signal in plan.signals:
        if signal.samples < min_samples:
            continue
        before = float(current.get(signal.topic, 1.0))
        # 평균 신호를 그대로 쓰지 않고 max_delta 범위로 눌러 담는다(천천히 조정).
        delta = max(-max_delta, min(max_delta, signal.average * max_delta))
        after = max(min_weight, min(max_weight, round(before + delta, 4)))
        if abs(after - before) < 1e-6:
            continue
        plan.changes.append(
            TopicChange(
                topic=signal.topic,
                before=before,
                after=after,
                delta=round(after - before, 4),
                samples=signal.samples,
                reason=", ".join(f"{k}×{v}" for k, v in sorted(signal.sources.items())),
            )
        )
        plan.new_weights[signal.topic] = after

    if not plan.changes:
        plan.status = STATUS_NO_CHANGE
        plan.message = "조정할 만큼 뚜렷한 topic 신호가 없습니다."
    return plan


def format_plan(plan: LearningPlan, active_version: str) -> str:
    """CLI 출력."""
    lines = [
        "학습 결과 (Topic Weight)",
        "",
        f"  현재 Profile : {active_version}",
        f"  Feedback 표본: {plan.feedback_count}",
        f"  상태         : {plan.status}",
    ]
    if plan.message:
        lines.append(f"  안내         : {plan.message}")
    if plan.changes:
        lines.append("")
        lines.append("  [변경 예정]")
        for change in plan.changes:
            lines.append(f"    {change.as_line()}")
            lines.append(f"      근거: {change.reason}")
    return "\n".join(lines)
