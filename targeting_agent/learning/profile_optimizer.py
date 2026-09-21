"""Profile/Weight 조정 제안기(v0.1: 제안만, 자동 반영 없음).

축적된 Feedback을 보고 scoring.weights 조정안을 계산한다.
실제 config.yaml을 자동으로 고치지 않는다 — 운영자가 확인 후 반영한다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Mapping

from ..core.models import FeedbackType
from .feedback import feedback_counts

# Feedback 타입 -> 영향을 주는 Score 구성요소
SIGNAL_MAP = {
    FeedbackType.GOOD_TARGET.value: ("content_similarity", +1),
    FeedbackType.BAD_TARGET.value: ("content_similarity", -1),
    FeedbackType.NOT_MY_STYLE.value: ("content_similarity", -1),
    FeedbackType.RESPONDED.value: ("engagement", +1),
    FeedbackType.FOLLOWED.value: ("creator_fit", +1),
}

MAX_STEP = 0.05  # 한 번에 조정할 수 있는 최대 가중치 변화량


@dataclass
class WeightSuggestion:
    current: dict[str, float] = field(default_factory=dict)
    suggested: dict[str, float] = field(default_factory=dict)
    rationale: list[str] = field(default_factory=list)
    sample_size: int = 0

    @property
    def has_change(self) -> bool:
        return any(
            abs(self.suggested.get(k, 0) - v) > 1e-6 for k, v in self.current.items()
        )


def suggest_weights(
    conn: sqlite3.Connection,
    current_weights: Mapping[str, float],
    *,
    min_samples: int = 10,
) -> WeightSuggestion:
    """Feedback 기반 가중치 조정안을 계산한다(합계 1.0 유지)."""
    counts = feedback_counts(conn)
    total = sum(counts.values())
    suggestion = WeightSuggestion(
        current=dict(current_weights), suggested=dict(current_weights), sample_size=total
    )
    if total < min_samples:
        suggestion.rationale.append(
            f"Feedback 표본이 부족합니다({total}/{min_samples}) — 가중치를 변경하지 않습니다."
        )
        return suggestion

    deltas: dict[str, float] = {}
    for feedback_type, count in counts.items():
        mapped = SIGNAL_MAP.get(feedback_type)
        if not mapped:
            continue
        component, direction = mapped
        deltas[component] = deltas.get(component, 0.0) + direction * (count / total)

    if not deltas:
        suggestion.rationale.append("가중치에 연결된 Feedback이 없습니다.")
        return suggestion

    adjusted = dict(current_weights)
    for component, delta in deltas.items():
        if component not in adjusted:
            continue
        step = max(-MAX_STEP, min(MAX_STEP, delta * MAX_STEP * 2))
        adjusted[component] = max(0.01, adjusted[component] + step)
        suggestion.rationale.append(f"{component}: {step:+.3f} (feedback 기반)")

    total_weight = sum(adjusted.values())
    suggestion.suggested = {k: round(v / total_weight, 4) for k, v in adjusted.items()}
    return suggestion
