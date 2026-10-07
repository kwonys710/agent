"""Target Quality Calibration (Phase 18D.2).

실제로 수집된 후보의 Target Score가 **왜 그 값인지**를 분해한다.
점수를 임계값에 맞추려고 가중치나 임계값을 건드리지 않는다 — 진단만 한다.

가장 중요한 구분:

    정보 부족(unknown)  ≠  실제 부적합(unfit)

Scorer는 정보가 없는 축을 중립값으로 채운다(followers를 모르면 creator_fit·
engagement = 0.5, posted_at을 모르면 activity = 0.5). 이 중립값은 "보통이다"가
아니라 "모른다"는 뜻인데, 가중합에서는 똑같이 절반만 받은 것처럼 계산된다.
그래서 정보가 없는 축의 가중치가 크면 **아무리 잘 맞는 후보라도 넘을 수 없는
점수 상한**이 생긴다. 그 상한을 후보마다 계산해 둔다.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from ..core.config import Config

BROWSER_SOURCE = "instagram_browser_search"

# Scorer가 "정보 없음"일 때 쓰는 중립값(analysis/scorer.py와 같은 값을 본다).
NEUTRAL_VALUES: Mapping[str, float] = {
    "creator_fit": 0.5,
    "activity": 0.5,
    "engagement": 0.5,
}
# 어떤 입력이 없으면 그 축을 모르게 되는지.
COMPONENT_INPUTS: Mapping[str, tuple[str, ...]] = {
    "creator_fit": ("followers",),
    "activity": ("posted_at",),
    "engagement": ("followers",),
}

# content_similarity가 이 아래면 "주제가 실제로 안 맞는다"고 본다(Discovery 문제).
LOW_SIMILARITY = 0.60
# 사람 Feedback이 이만큼은 있어야 보정 신호로 쓴다.
MIN_GROUND_TRUTH = 10

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_INSUFFICIENT = "INSUFFICIENT_HUMAN_FEEDBACK"

# 저점수 원인(진단 라벨). 새 enum을 늘리지 않고 문자열로 둔다.
REASON_METADATA_MISSING = "metadata_missing"
REASON_LOW_SIMILARITY = "low_similarity"
REASON_LANGUAGE = "language_mismatch"
REASON_CREATOR_RANGE = "creator_out_of_range"
REASON_LOW_ENGAGEMENT = "low_engagement"
REASON_UNFIT = "genuinely_unfit"

SCORE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("90+", 90.0, 1000.0),
    ("80-89", 80.0, 90.0),
    ("75-79", 75.0, 80.0),
    ("70-74", 70.0, 75.0),
    ("60-69", 60.0, 70.0),
    ("<60", -1.0, 60.0),
)

_CANDIDATE_SQL = """
SELECT m.media_pk, m.media_id, m.source, m.posted_at, m.like_count, m.comment_count,
       a.target_score, a.score_breakdown, a.analyzer,
       c.followers, c.is_private, c.last_interacted_at
FROM candidate_media m
JOIN media_analysis a ON a.media_pk = m.media_pk
LEFT JOIN creators c ON c.creator_id = m.creator_id
WHERE a.target_score IS NOT NULL
ORDER BY m.media_pk, a.analysis_id DESC
"""

# 사람이 남긴 신호만 센다. Autopilot이 스스로 승인한 것은 Ground Truth가 아니다.
_HUMAN_FEEDBACK_SQL = "SELECT feedback_type, COUNT(*) FROM feedback GROUP BY feedback_type"
_HUMAN_APPROVAL_SQL = (
    "SELECT COUNT(*) FROM action_queue WHERE approved_at IS NOT NULL AND approved_by = 'operator'"
)


@dataclass
class CandidateScore:
    """후보 한 건의 점수 분해."""

    media_pk: int
    media_id: str
    source: str
    score: float
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()

    @property
    def browser(self) -> bool:
        return self.source == BROWSER_SOURCE

    @property
    def ceiling(self) -> float:
        """아는 축이 **전부 만점**이어도 넘을 수 없는 점수.

        모르는 축은 중립값에 고정돼 있으므로 그만큼은 영원히 못 받는다.
        """
        total = 0.0
        for name, weight in self.weights.items():
            best = NEUTRAL_VALUES.get(name, 1.0) if name in self.unknown else 1.0
            total += weight * best
        return round(total * 100, 2)

    @property
    def unknown_weight(self) -> float:
        """모르는 축이 차지하는 가중치 합(0~1)."""
        return round(sum(self.weights.get(name, 0.0) for name in self.unknown), 4)

    @property
    def headroom(self) -> float:
        """빠진 정보를 채웠을 때 **더 받을 수 있는** 점수 폭.

        중립값(0.5)은 "보통"이 아니라 "모른다"는 뜻이다. 정보를 알게 되면
        그 축은 0에서 1 사이 어디로든 갈 수 있고, 위쪽으로 남은 여유가 이 값이다.
        """
        total = 0.0
        for name in self.unknown:
            total += self.weights.get(name, 0.0) * (1.0 - NEUTRAL_VALUES.get(name, 0.5))
        return round(total * 100, 2)

    @property
    def potential_max(self) -> float:
        """빠진 정보가 모두 유리하게 나왔을 때의 점수."""
        return round(self.score + self.headroom, 2)

    def metadata_decisive(self, threshold: float) -> bool:
        """임계값 통과 여부를 **빠진 정보가 좌우하는지**.

        지금은 못 넘지만 정보를 채우면 넘을 수 있다면, 이 후보를 떨어뜨린 것은
        '부적합'이 아니라 '모른다'다. 둘을 같은 칸에 넣으면 안 된다.
        """
        return bool(self.unknown) and self.score < threshold <= self.potential_max

    def reason(self, *, threshold: float) -> str:
        """저점수의 주된 원인 하나를 고른다(진단용 라벨)."""
        if self.components.get("content_similarity", 1.0) < LOW_SIMILARITY:
            return REASON_LOW_SIMILARITY
        if self.metadata_decisive(threshold):
            return REASON_METADATA_MISSING
        if self.components.get("language_region", 1.0) < 0.6:
            return REASON_LANGUAGE
        if "creator_fit" not in self.unknown and self.components.get("creator_fit", 1.0) < 0.5:
            return REASON_CREATOR_RANGE
        if "engagement" not in self.unknown and self.components.get("engagement", 1.0) < 0.4:
            return REASON_LOW_ENGAGEMENT
        return REASON_UNFIT

    def as_row(self, *, threshold: float) -> dict[str, str]:
        return {
            "media_id": self.media_id,
            "source": "browser" if self.browser else self.source,
            "score": f"{self.score:.2f}",
            "ceiling": f"{self.ceiling:.2f}",
            "잠재최대": f"{self.potential_max:.2f}",
            "unknown": ",".join(self.unknown) or "-",
            "similarity": f"{self.components.get('content_similarity', 0):.2f}",
            "reason": self.reason(threshold=threshold),
        }


@dataclass
class ComponentStat:
    """구성요소 한 축의 분포."""

    name: str
    values: list[float] = field(default_factory=list)
    neutral_count: int = 0
    unknown_count: int = 0

    def _quantile(self, fraction: float) -> Optional[float]:
        if not self.values:
            return None
        ordered = sorted(self.values)
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    def as_row(self) -> str:
        if not self.values:
            return "표본 없음"
        return (
            f"mean {statistics.mean(self.values):.3f} · "
            f"median {statistics.median(self.values):.3f} · "
            f"P25 {self._quantile(0.25):.3f} · P75 {self._quantile(0.75):.3f} · "
            f"min {min(self.values):.2f} · max {max(self.values):.2f} · "
            f"정보없음 {self.unknown_count}/{len(self.values)}"
        )


@dataclass
class ScoreCalibration:
    """Phase 18D.2 결과. 설정을 바꾸지 않고 진단과 추천만 담는다."""

    like_threshold: float = 75.0
    comment_threshold: float = 82.0
    minimum_score: float = 70.0
    candidates: list[CandidateScore] = field(default_factory=list)
    bands: dict[str, int] = field(default_factory=dict)
    bands_browser: dict[str, int] = field(default_factory=dict)
    components: dict[str, ComponentStat] = field(default_factory=dict)
    human_feedback: dict[str, int] = field(default_factory=dict)
    human_approvals: int = 0
    findings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.candidates)

    @property
    def browser_candidates(self) -> list[CandidateScore]:
        return [item for item in self.candidates if item.browser]

    @property
    def ground_truth_count(self) -> int:
        """사람이 남긴 신호의 수(Autopilot 자동 승인은 제외한다)."""
        return sum(self.human_feedback.values()) + self.human_approvals

    @property
    def confidence(self) -> str:
        if self.ground_truth_count >= MIN_GROUND_TRUTH:
            return CONFIDENCE_HIGH
        if self.ground_truth_count > 0:
            return CONFIDENCE_LOW
        return CONFIDENCE_INSUFFICIENT

    def unreachable(self, threshold: float) -> list[CandidateScore]:
        """정보가 빠져 있어 그 임계값에 **구조적으로** 닿을 수 없는 후보.

        아는 축이 전부 만점이어도 상한이 임계값에 못 미치는 경우다.
        """
        return [item for item in self.candidates if item.ceiling < threshold]

    @property
    def comment_unreachable(self) -> list[CandidateScore]:
        return self.unreachable(self.comment_threshold)

    @property
    def metadata_limited(self) -> list[CandidateScore]:
        """정보만 채우면 minimum_score를 넘을 수 있었던 후보."""
        return [
            item for item in self.candidates if item.metadata_decisive(self.minimum_score)
        ]

    def scores(self) -> list[float]:
        return [item.score for item in self.candidates]

    def low_score_reasons(self, limit: int = 20) -> dict[str, int]:
        """임계값 미만 후보의 원인 분포(표본은 limit건까지만 본다)."""
        low = [item for item in self.candidates if item.score < self.minimum_score][:limit]
        counts: dict[str, int] = {}
        for item in low:
            reason = item.reason(threshold=self.minimum_score)
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def as_rows(self) -> dict[str, str]:
        values = self.scores()
        summary = (
            f"mean {statistics.mean(values):.1f} · median {statistics.median(values):.1f} · "
            f"min {min(values):.1f} · max {max(values):.1f}"
            if values
            else "표본 없음"
        )
        rows = {
            "채점된 후보": f"{self.total} (browser {len(self.browser_candidates)})",
            "Score 요약": summary,
            "Score 분포": ", ".join(f"{band}={count}" for band, count in self.bands.items()),
            "Score 분포(browser)": ", ".join(
                f"{band}={count}" for band, count in self.bands_browser.items()
            ),
            "임계값": (
                f"LIKE {self.like_threshold:.0f} · COMMENT {self.comment_threshold:.0f} · "
                f"minimum {self.minimum_score:.0f}"
            ),
            "COMMENT 도달 불가(정보부족)": f"{len(self.comment_unreachable)} / {self.total}",
            "LIKE 도달 불가(정보부족)": f"{len(self.unreachable(self.like_threshold))} / {self.total}",
            "정보만 채우면 통과 가능": f"{len(self.metadata_limited)} / {self.total}",
            "저점수 원인": ", ".join(
                f"{reason}={count}" for reason, count in sorted(self.low_score_reasons().items())
            )
            or "없음",
            "사람 Ground Truth": f"{self.ground_truth_count}건 (신뢰도 {self.confidence})",
        }
        for name, stat in self.components.items():
            rows[f"축 · {name}"] = stat.as_row()
        return rows


def _unknown_components(row: Mapping[str, object]) -> tuple[str, ...]:
    """이 후보에서 '정보가 없어서 중립값이 된' 축을 찾는다."""
    missing: list[str] = []
    for component, inputs in COMPONENT_INPUTS.items():
        for field_name in inputs:
            value = row.get(field_name)
            absent = value in (None, "", 0) or (
                isinstance(value, str) and not value.strip()
            )
            if absent:
                missing.append(component)
                break
    return tuple(missing)


def _band_of(score: float) -> str:
    for label, low, high in SCORE_BANDS:
        if low <= score < high:
            return label
    return SCORE_BANDS[-1][0]


def _count_bands(items: Iterable[CandidateScore]) -> dict[str, int]:
    counts = {label: 0 for label, _, _ in SCORE_BANDS}
    for item in items:
        counts[_band_of(item.score)] += 1
    return counts


def collect_calibration(conn: sqlite3.Connection, config: Config) -> ScoreCalibration:
    """실제 후보의 점수를 분해한다. **쓰기도 LLM 호출도 하지 않는다.**"""
    result = ScoreCalibration(
        like_threshold=float(config.get("actions.require_score_for_like", 75)),
        comment_threshold=float(config.get("actions.require_score_for_comment", 82)),
        minimum_score=float(config.get("scoring.minimum_target_score", 70)),
    )

    seen: set[int] = set()
    cursor = conn.execute(_CANDIDATE_SQL)
    columns = [description[0] for description in cursor.description]
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        media_pk = int(row["media_pk"])
        if media_pk in seen:
            continue  # 같은 후보의 이전 분석은 건너뛴다(가장 최근 것만 본다)
        seen.add(media_pk)
        breakdown = json.loads(row["score_breakdown"] or "{}")
        components = {str(k): float(v) for k, v in (breakdown.get("components") or {}).items()}
        if not components:
            continue
        result.candidates.append(
            CandidateScore(
                media_pk=media_pk,
                media_id=str(row["media_id"] or ""),
                source=str(row["source"] or ""),
                score=float(row["target_score"]),
                components=components,
                weights={str(k): float(v) for k, v in (breakdown.get("weights") or {}).items()},
                unknown=_unknown_components(row),
            )
        )

    result.bands = _count_bands(result.candidates)
    result.bands_browser = _count_bands(result.browser_candidates)

    for item in result.candidates:
        for name, value in item.components.items():
            stat = result.components.setdefault(name, ComponentStat(name=name))
            stat.values.append(value)
            if name in item.unknown:
                stat.unknown_count += 1
            if abs(value - NEUTRAL_VALUES.get(name, -1.0)) < 1e-9:
                stat.neutral_count += 1

    result.human_feedback = {
        str(key): int(count) for key, count in conn.execute(_HUMAN_FEEDBACK_SQL).fetchall()
    }
    row = conn.execute(_HUMAN_APPROVAL_SQL).fetchone()
    result.human_approvals = int(row[0]) if row else 0

    _add_findings(result)
    return result


def _add_findings(result: ScoreCalibration) -> None:
    """측정값에서 바로 읽히는 사실만 적는다(추측하지 않는다)."""
    browser = result.browser_candidates
    if browser:
        # 축마다 "몇 건이 그 정보를 모르는지"를 센다. 후보마다 빠진 축이 달라서
        # 축 목록과 최고 상한을 한 문장에 섞으면 사실과 어긋난다.
        missing: dict[str, int] = {}
        for item in browser:
            for axis in item.unknown:
                missing[axis] = missing.get(axis, 0) + 1
        if missing:
            detail = ", ".join(f"{axis} {count}/{len(browser)}" for axis, count in sorted(missing.items()))
            result.findings.append(f"Browser 후보에서 정보가 빠진 축: {detail}.")

        best = max(item.ceiling for item in browser)
        worst = min(item.ceiling for item in browser)
        result.findings.append(
            f"Browser 후보의 점수 상한은 {worst:.1f}~{best:.1f}점입니다 "
            "(빠진 축이 중립값에 묶여 있어 생기는 한계)."
        )
        blocked_comment = [item for item in browser if item.ceiling < result.comment_threshold]
        if blocked_comment:
            result.findings.append(
                f"그중 {len(blocked_comment)}/{len(browser)}건은 상한이 COMMENT 임계값"
                f"({result.comment_threshold:.0f})보다 낮습니다 — 정보를 채우기 전에는 "
                "COMMENT 대상이 될 수 없습니다."
            )
        blocked_like = [item for item in browser if item.ceiling <= result.like_threshold]
        if blocked_like:
            result.findings.append(
                f"{len(blocked_like)}/{len(browser)}건은 상한이 LIKE 임계값"
                f"({result.like_threshold:.0f})과 같거나 낮습니다 — 다른 축이 동시에 "
                "만점일 때만 겨우 닿습니다."
            )
        limited = [item for item in browser if item.metadata_decisive(result.minimum_score)]
        if limited:
            result.findings.append(
                f"지금 minimum({result.minimum_score:.0f}) 미만인 Browser 후보 중 {len(limited)}건은 "
                "빠진 정보를 채우면 통과할 수 있었습니다 — 부적합이 아니라 '모름'입니다."
            )

    reasons = result.low_score_reasons()
    similarity_low = reasons.get(REASON_LOW_SIMILARITY, 0)
    if similarity_low:
        result.findings.append(
            f"주제가 실제로 맞지 않는 후보가 {similarity_low}건입니다 "
            "(content_similarity < 0.60) — 이건 Scoring이 아니라 Discovery 쪽 문제입니다."
        )

    if result.confidence == CONFIDENCE_INSUFFICIENT:
        result.findings.append(
            "사람이 남긴 Ground Truth가 없습니다 — 임계값을 바꿀 근거가 없습니다."
        )
    elif result.confidence == CONFIDENCE_LOW:
        result.findings.append(
            f"사람 Ground Truth가 {result.ground_truth_count}건뿐입니다"
            f"(최소 {MIN_GROUND_TRUTH}건 필요) — 임계값 조정은 보류합니다."
        )

    # 추천은 문장으로만 남긴다. 설정은 건드리지 않는다.
    if result.comment_unreachable or result.metadata_limited:
        result.recommendations.append(
            "임계값을 내리는 대신 빠진 정보를 채우는 쪽을 먼저 보세요 — "
            "작성자 followers와 게시 시각을 알면 상한 자체가 사라집니다."
        )
    if result.confidence != CONFIDENCE_HIGH:
        result.recommendations.append(
            f"LIKE {result.like_threshold:.0f} / COMMENT {result.comment_threshold:.0f} 유지 — "
            "사람 Feedback이 쌓이기 전에는 임계값을 바꿀 근거가 없습니다."
        )
    result.recommendations.append("※ 추천일 뿐이며 config는 자동으로 변경되지 않습니다.")


def sample_rows(
    result: ScoreCalibration, *, limit: int = 20
) -> Sequence[dict[str, str]]:
    """임계값 미만 후보 표본(최대 limit건)."""
    low = sorted(
        (item for item in result.candidates if item.score < result.minimum_score),
        key=lambda item: item.score,
    )
    return [item.as_row(threshold=result.minimum_score) for item in low[:limit]]
