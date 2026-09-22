"""Feedback → Target Profile / Score 가중치 반영 (Phase 10).

v0.1은 Machine Learning 모델을 만들지 않는다. 대신:
1) 축적된 Feedback을 집계해
2) Target Profile 키워드와 scoring.weights 조정안을 계산하고
3) 운영자가 승인하면(`--learn-apply`) app_state에 override로 저장한다.

config.yaml은 자동으로 고치지 않는다. 학습 결과는 DB override이며
`--learn-reset`으로 언제든 되돌릴 수 있다.

가중치나 Profile이 바뀌면 Profile version을 올려 분석/점수 캐시를 무효화한다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..analysis.profile_analyzer import TargetProfile, bump_version, profile_to_mapping
from ..analysis.similarity import STOPWORDS
from ..core.database import get_state, set_state, transaction, utc_now
from ..core.logger import get_logger
from ..core.models import FeedbackType
from .feedback import feedback_counts

logger = get_logger("learning.optimizer")

PROFILE_STATE_KEY = "target_profile"
WEIGHTS_STATE_KEY = "scoring_weights"
UPDATED_AT_KEY = "profile_updated_at"

# Feedback 타입 -> 영향을 주는 Score 구성요소
SIGNAL_MAP = {
    FeedbackType.GOOD_TARGET.value: ("content_similarity", +1),
    FeedbackType.BAD_TARGET.value: ("content_similarity", -1),
    FeedbackType.NOT_MY_STYLE.value: ("content_similarity", -1),
    FeedbackType.RESPONDED.value: ("engagement", +1),
    FeedbackType.FOLLOWED.value: ("creator_fit", +1),
}

# 키워드 학습 신호 강도.
# LIKED/COMMENTED는 '내가 한 행동'일 뿐 상대 반응이 아니므로 약한 신호로만 센다.
KEYWORD_SIGNALS: dict[str, float] = {
    FeedbackType.GOOD_TARGET.value: 1.0,
    FeedbackType.RESPONDED.value: 1.5,
    FeedbackType.FOLLOWED.value: 1.5,
    FeedbackType.GOOD_COMMENT.value: 0.5,
    FeedbackType.LIKED.value: 0.2,
    FeedbackType.COMMENTED.value: 0.2,
    FeedbackType.BAD_TARGET.value: -1.0,
    FeedbackType.NOT_MY_STYLE.value: -1.5,
    FeedbackType.BAD_COMMENT.value: -0.3,
}

MAX_STEP = 0.05          # 한 번에 조정할 수 있는 최대 가중치 변화량
MAX_NEW_KEYWORDS = 5     # 한 번에 Profile에 추가할 수 있는 키워드 수
MAX_NEW_AVOID = 5


# --- 가중치 --------------------------------------------------------------
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


def load_weights_override(conn: sqlite3.Connection) -> Optional[dict[str, float]]:
    """학습으로 저장된 가중치 override를 읽는다(없으면 None)."""
    raw = get_state(conn, WEIGHTS_STATE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("app_state.%s JSON 파싱 실패 — config 가중치를 사용합니다.", WEIGHTS_STATE_KEY)
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): float(v) for k, v in data.items()}


# --- 키워드 --------------------------------------------------------------
@dataclass
class KeywordSignal:
    keyword: str
    score: float = 0.0
    occurrences: int = 0            # 이 키워드에 영향을 준 Feedback 건수
    media: set[str] = field(default_factory=set)  # 이 키워드가 등장한 서로 다른 게시물

    @property
    def media_count(self) -> int:
        return len(self.media)


@dataclass
class ProfileSuggestion:
    add_keywords: list[str] = field(default_factory=list)
    add_avoid: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    sample_size: int = 0
    signals: list[KeywordSignal] = field(default_factory=list)
    # 운영자가 직접 선언한 Profile과 어긋나는 신호(자동 반영하지 않는다)
    conflicts: list[str] = field(default_factory=list)

    @property
    def has_change(self) -> bool:
        return bool(self.add_keywords or self.add_avoid)


def _hashtags_of(raw: Any) -> list[str]:
    """candidate_media.hashtags(JSON 배열)를 리스트로 읽는다."""
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(t) for t in data] if isinstance(data, list) else []


def _topics_only(payload: Mapping[str, Any]) -> list[str]:
    """해시태그가 없으면 분석 토픽만 사용한다(캡션 토큰은 쓰지 않는다)."""
    topics = payload.get("topics") or []
    return [str(t) for t in topics if str(t) != "기타"]


def _relates_to(keyword: str, known: Iterable[str]) -> Optional[str]:
    """키워드가 기존 목록의 항목과 사실상 같은 대상인지 확인한다.

    '카페'가 Profile에 있는데 '카페에서'를 회피 목록에 넣으면 결과적으로
    Profile 키워드를 무력화한다. 부분 일치까지 같은 대상으로 본다.
    """
    key = keyword.lower()
    for item in known:
        other = str(item).lower()
        if not other:
            continue
        if key == other or key in other or other in key:
            return other
    return None


def collect_keyword_signals(conn: sqlite3.Connection) -> tuple[dict[str, KeywordSignal], int]:
    """Feedback이 달린 media의 **해시태그**를 신호 강도로 집계한다.

    캡션 토큰까지 학습하면 '보는', '오늘도' 같은 어미/조사가 Profile에 들어간다.
    해시태그는 작성자가 의도적으로 붙인 주제 라벨이므로 학습 신호로 더 적합하다.
    """
    rows = conn.execute(
        "SELECT f.feedback_type, f.media_pk, a.payload, m.hashtags FROM feedback f "
        "JOIN media_analysis a ON a.media_pk = f.media_pk "
        "JOIN candidate_media m ON m.media_pk = f.media_pk "
        "WHERE f.media_pk IS NOT NULL"
    ).fetchall()

    signals: dict[str, KeywordSignal] = {}
    samples = 0
    for row in rows:
        weight = KEYWORD_SIGNALS.get(str(row["feedback_type"]))
        if not weight:
            continue
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        keywords = _hashtags_of(row["hashtags"]) or _topics_only(payload)
        if not keywords:
            continue
        samples += 1
        for keyword in keywords:
            key = str(keyword).strip().lower()
            if len(key) < 2 or key.isdigit() or key in STOPWORDS:
                continue
            signal = signals.setdefault(key, KeywordSignal(keyword=key))
            signal.score += weight
            signal.occurrences += 1
            signal.media.add(str(row["media_pk"]))
    return signals, samples


def suggest_profile(
    conn: sqlite3.Connection,
    profile: TargetProfile,
    *,
    min_samples: int = 10,
    min_occurrences: int = 3,
    min_score: float = 2.0,
    min_media: int = 2,
) -> ProfileSuggestion:
    """Feedback 키워드 집계로 Profile 키워드 추가/회피 후보를 계산한다."""
    signals, samples = collect_keyword_signals(conn)
    suggestion = ProfileSuggestion(sample_size=samples)
    suggestion.signals = sorted(signals.values(), key=lambda s: s.score, reverse=True)

    if samples < min_samples:
        suggestion.rationale.append(
            f"Feedback 표본이 부족합니다({samples}/{min_samples}) — Profile을 변경하지 않습니다."
        )
        return suggestion

    known = {k.lower() for k in profile.all_keywords}
    avoided = {k.lower() for k in profile.avoid_keywords}

    for signal in suggestion.signals:
        if signal.occurrences < min_occurrences:
            continue
        if signal.media_count < min_media:
            # 게시물 한 건에서만 나온 단어로 Profile을 바꾸지 않는다.
            continue

        related_known = _relates_to(signal.keyword, known)
        related_avoided = _relates_to(signal.keyword, avoided)

        if signal.score >= min_score:
            if related_avoided:
                # 운영자가 회피하기로 정한 키워드를 학습이 되살리지 않는다.
                suggestion.conflicts.append(
                    f"{signal.keyword}: 긍정 신호({signal.score:+.1f})지만 회피 키워드"
                    f"('{related_avoided}')와 겹침 — profile 파일에서 직접 확인하세요"
                )
                continue
            if related_known:
                continue  # 이미 Profile이 다루는 키워드
            if len(suggestion.add_keywords) < MAX_NEW_KEYWORDS:
                suggestion.add_keywords.append(signal.keyword)
                suggestion.rationale.append(
                    f"+{signal.keyword} (점수 {signal.score:+.1f}, "
                    f"{signal.occurrences}건 / 게시물 {signal.media_count}개)"
                )

        elif signal.score <= -min_score:
            if related_known:
                # Profile 핵심 키워드를 소수의 Feedback으로 회피 처리하면
                # 운영자가 선언한 콘텐츠 방향이 통째로 뒤집힌다. 보고만 한다.
                suggestion.conflicts.append(
                    f"{signal.keyword}: 부정 신호({signal.score:+.1f}, {signal.occurrences}건)지만 "
                    f"Profile 키워드('{related_known}')와 겹침 — 빼려면 profile 파일에서 직접 수정하세요"
                )
                continue
            if related_avoided:
                continue
            if len(suggestion.add_avoid) < MAX_NEW_AVOID:
                suggestion.add_avoid.append(signal.keyword)
                suggestion.rationale.append(
                    f"-{signal.keyword} (점수 {signal.score:+.1f}, "
                    f"{signal.occurrences}건 / 게시물 {signal.media_count}개) → 회피"
                )

    if not suggestion.has_change:
        suggestion.rationale.append("조정할 만큼 뚜렷한 키워드 신호가 없습니다.")
    return suggestion


# --- 적용 / 되돌리기 ------------------------------------------------------
def should_update(
    conn: sqlite3.Connection, interval_days: int, now: Optional[datetime] = None
) -> bool:
    """마지막 적용 이후 설정한 주기가 지났는지 확인한다."""
    if interval_days <= 0:
        return True
    raw = get_state(conn, UPDATED_AT_KEY)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last >= timedelta(days=interval_days)


def apply_learning(
    conn: sqlite3.Connection,
    profile: TargetProfile,
    profile_suggestion: ProfileSuggestion,
    weight_suggestion: Optional[WeightSuggestion] = None,
) -> dict[str, Any]:
    """제안을 app_state에 반영한다(config.yaml은 건드리지 않는다)."""
    applied: dict[str, Any] = {"keywords": [], "avoid": [], "weights": None, "version": profile.version}

    data = profile_to_mapping(profile)
    changed = False

    if profile_suggestion.add_keywords:
        shared = list(profile.shared_keywords) + [
            k for k in profile_suggestion.add_keywords if k not in profile.shared_keywords
        ]
        data["shared"] = {**dict(data.get("shared") or {}), "keywords": shared}
        applied["keywords"] = list(profile_suggestion.add_keywords)
        changed = True

    if profile_suggestion.add_avoid:
        avoid = list(profile.avoid_keywords) + [
            k for k in profile_suggestion.add_avoid if k not in profile.avoid_keywords
        ]
        data["avoid_keywords"] = avoid
        applied["avoid"] = list(profile_suggestion.add_avoid)
        changed = True

    if changed:
        # 버전을 올려야 분석/점수 캐시가 무효화되고 다음 실행에서 다시 계산된다.
        data["version"] = bump_version(profile.version)
        applied["version"] = data["version"]

    with transaction(conn):
        if changed:
            set_state(conn, PROFILE_STATE_KEY, json.dumps(data, ensure_ascii=False))
        if weight_suggestion is not None and weight_suggestion.has_change:
            set_state(
                conn,
                WEIGHTS_STATE_KEY,
                json.dumps(weight_suggestion.suggested, ensure_ascii=False),
            )
            applied["weights"] = dict(weight_suggestion.suggested)
            changed = True
        if changed:
            set_state(conn, UPDATED_AT_KEY, utc_now())

    if changed:
        logger.info(
            "학습 결과 반영: 키워드 %d개, 회피 %d개, 가중치 %s",
            len(applied["keywords"]),
            len(applied["avoid"]),
            "변경" if applied["weights"] else "유지",
        )
    else:
        logger.info("반영할 학습 결과가 없습니다.")
    return applied


def reset_learning(conn: sqlite3.Connection) -> None:
    """학습 override를 모두 제거하고 config.yaml / profile 파일 기준으로 되돌린다."""
    with transaction(conn):
        for key in (PROFILE_STATE_KEY, WEIGHTS_STATE_KEY, UPDATED_AT_KEY):
            conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
    logger.info("학습 override를 초기화했습니다.")


def format_suggestions(
    profile_suggestion: ProfileSuggestion,
    weight_suggestion: WeightSuggestion,
    *,
    applied: Optional[dict[str, Any]] = None,
) -> str:
    """CLI 출력용 텍스트."""
    lines = ["학습 제안 (Feedback 기반)", ""]
    lines.append(f"  Profile 표본: {profile_suggestion.sample_size}건")
    lines.append(f"  Feedback 총계: {weight_suggestion.sample_size}건")
    lines.append("")

    lines.append("  [Profile 키워드]")
    if profile_suggestion.has_change:
        if profile_suggestion.add_keywords:
            lines.append(f"    추가: {', '.join(profile_suggestion.add_keywords)}")
        if profile_suggestion.add_avoid:
            lines.append(f"    회피: {', '.join(profile_suggestion.add_avoid)}")
    for reason in profile_suggestion.rationale:
        lines.append(f"    - {reason}")

    if profile_suggestion.conflicts:
        lines.append("")
        lines.append("  [확인 필요 — 자동 반영하지 않음]")
        for conflict in profile_suggestion.conflicts:
            lines.append(f"    ! {conflict}")

    lines.append("")
    lines.append("  [Score 가중치]")
    if weight_suggestion.has_change:
        for name, value in weight_suggestion.suggested.items():
            before = weight_suggestion.current.get(name, 0.0)
            mark = "" if abs(value - before) < 1e-6 else f"  ({before:.3f} → {value:.3f})"
            lines.append(f"    {name}: {value:.4f}{mark}")
    for reason in weight_suggestion.rationale:
        lines.append(f"    - {reason}")

    if applied is not None:
        lines.append("")
        if applied.get("keywords") or applied.get("avoid") or applied.get("weights"):
            lines.append(f"  → 반영 완료 (Profile version {applied.get('version')})")
        else:
            lines.append("  → 반영할 변경이 없습니다.")
    else:
        lines.append("")
        lines.append("  (반영하려면 --learn-apply, 되돌리려면 --learn-reset)")
    return "\n".join(lines)
