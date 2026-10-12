"""Target Score 계산(0~100).

구성요소는 모두 0~1로 정규화한 뒤 config.scoring.weights로 가중합한다.
- content_similarity : DailyReels Profile과의 콘텐츠 유사도
- creator_fit        : 팔로워 규모/공개 여부 적합도
- activity           : 게시물 최신성
- engagement         : 팔로워 대비 반응률
- language_region    : 목표 언어 일치
- novelty            : 처음 만나는 Creator인지(중복 Interaction 회피)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from ..core.config import Config
from ..core.models import ContentAnalysis, ScoreBreakdown
from .profile_analyzer import TargetProfile, creator_fit
from .similarity import profile_similarity


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (
        lambda t: datetime.fromisoformat(t),
        lambda t: datetime.strptime(t, "%Y-%m-%d"),
        lambda t: datetime.strptime(t, "%Y/%m/%d"),
    ):
        try:
            parsed = parser(text)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def activity_score(posted_at: Any, *, half_life_days: float = 14.0) -> float:
    """최신성 점수. 정보가 없으면 중립값 0.5."""
    parsed = _parse_datetime(posted_at)
    if parsed is None:
        return 0.5
    age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    if age_days < 0:
        return 1.0
    return round(max(0.0, min(1.0, 0.5 ** (age_days / half_life_days))), 4)


def engagement_score(likes: int, comments: int, followers: int) -> float:
    """팔로워 대비 반응률. 5% 이상이면 만점으로 본다."""
    if followers <= 0:
        return 0.5
    rate = (max(likes, 0) + max(comments, 0) * 3) / followers
    return round(min(1.0, rate / 0.05), 4)


def language_score(analysis_language: str, target_languages: Sequence[str]) -> float:
    if not target_languages:
        return 1.0
    if analysis_language in target_languages:
        return 1.0
    if analysis_language == "mixed":
        return 0.6
    if analysis_language == "unknown":
        return 0.4
    return 0.0


def novelty_score(last_interacted_at: Any, *, cooldown_days: int = 7) -> float:
    """처음 보는 Creator면 1.0, 최근에 접촉했을수록 낮다."""
    parsed = _parse_datetime(last_interacted_at)
    if parsed is None:
        return 1.0
    days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    if days >= cooldown_days:
        return 0.7
    return round(max(0.0, days / max(cooldown_days, 1) * 0.7), 4)


class TargetScorer:
    """config + profile 기반 Target Score 계산기."""

    def __init__(
        self,
        config: Config,
        profile: TargetProfile,
        weights_override: Optional[Mapping[str, float]] = None,
        topic_weights: Optional[Mapping[str, float]] = None,
        profile_version: str = "",
    ) -> None:
        self.config = config
        self.profile = profile
        # 학습 결과로 조정된 가중치가 있으면 그것을 우선 사용한다(app_state.scoring_weights).
        base = config.section("scoring.weights")
        self.weights = {
            k: float(weights_override[k]) if weights_override and k in weights_override else float(v)
            for k, v in base.items()
        }
        self.weights_source = "learned" if weights_override else "config"
        # 학습된 topic 가중치. content_similarity 한 축에만 곱한다(점수 공식은 그대로).
        self.topic_weights = {str(k): float(v) for k, v in (topic_weights or {}).items()}
        self.profile_version = profile_version
        self.languages = [str(l) for l in config.list_of("discovery.languages")]
        self.min_followers = int(config.get("discovery.creator.min_followers", 0))
        self.max_followers = int(config.get("discovery.creator.max_followers", 10**9))
        self.cooldown_days = int(config.get("actions.per_creator.cooldown_days", 7))

    def score(
        self,
        media_row: Mapping[str, Any],
        analysis: ContentAnalysis,
    ) -> ScoreBreakdown:
        """한 후보의 Target Score를 계산한다."""
        notes: list[str] = []
        # Claude가 판단한 의미적 관련성이 있으면 그것을 쓰고, 없으면 규칙 기반 유사도를 쓴다.
        # 나머지 구성요소와 최종 점수는 항상 Python이 결정론적으로 계산한다.
        if analysis.relevance_score is not None:
            content_similarity = max(0.0, min(1.0, float(analysis.relevance_score)))
            notes.append("similarity:claude")
        else:
            content_similarity = profile_similarity(
                analysis.keywords,
                analysis.topics,
                str(media_row.get("caption") or ""),
                self.profile.all_keywords,
                self.profile.topics,
            )
            notes.append("similarity:heuristic")

        topic_factor = self._topic_factor(analysis.topics)
        if abs(topic_factor - 1.0) > 1e-9:
            adjusted = max(0.0, min(1.0, content_similarity * topic_factor))
            notes.append(f"topic_weight:{topic_factor:.2f}")
            content_similarity = adjusted

        components = {
            "content_similarity": content_similarity,
            "creator_fit": creator_fit(
                int(media_row.get("followers") or 0),
                min_followers=self.min_followers,
                max_followers=self.max_followers,
                is_private=bool(media_row.get("is_private")),
            ),
            "activity": activity_score(media_row.get("posted_at")),
            "engagement": engagement_score(
                int(media_row.get("like_count") or 0),
                int(media_row.get("comment_count") or 0),
                int(media_row.get("followers") or 0),
            ),
            "language_region": language_score(analysis.language, self.languages),
            "novelty": novelty_score(
                media_row.get("last_interacted_at"), cooldown_days=self.cooldown_days
            ),
        }

        total = sum(self.weights.get(name, 0.0) * value for name, value in components.items())
        score = round(total * 100, 2)

        avoided = self.profile.is_avoided(list(analysis.keywords) + list(analysis.topics))
        if avoided:
            notes.append(f"avoid_keyword:{avoided}")
            score = 0.0
        if analysis.is_ad:
            notes.append("ad")
        if analysis.is_sensitive:
            notes.append("sensitive")

        return ScoreBreakdown(
            components={k: round(v, 4) for k, v in components.items()},
            weights=dict(self.weights),
            total=score,
            notes=notes,
            profile_version=self.profile_version,
        )

    def _topic_factor(self, topics: Sequence[str]) -> float:
        """후보 topic들의 학습 가중치 평균(없으면 1.0)."""
        if not self.topic_weights or not topics:
            return 1.0
        matched = [self.topic_weights[t] for t in topics if t in self.topic_weights]
        if not matched:
            return 1.0
        return sum(matched) / len(matched)


def disqualify_reason(
    config: Config, media_row: Mapping[str, Any], analysis: ContentAnalysis, breakdown: ScoreBreakdown
) -> Optional[str]:
    """safety 설정 기준으로 아예 대상에서 제외할 사유를 반환한다."""
    if bool(config.get("safety.skip_private_accounts", True)) and media_row.get("is_private"):
        return "private_account"
    if bool(config.get("safety.skip_ads", True)) and analysis.is_ad:
        return "ad_content"
    if bool(config.get("safety.skip_sensitive_content", True)) and analysis.is_sensitive:
        return "sensitive_content"
    for note in breakdown.notes:
        if note.startswith("avoid_keyword"):
            return note
    return None


def persist_score(
    conn: sqlite3.Connection,
    media_pk: int,
    analysis: ContentAnalysis,
    breakdown: ScoreBreakdown,
) -> None:
    """분석 캐시 행에 similarity/target_score를 갱신한다."""
    from ..core.database import save_analysis

    save_analysis(
        conn,
        media_pk,
        analysis,
        similarity=breakdown.components.get("content_similarity"),
        target_score=breakdown.total,
        score_breakdown=breakdown.to_dict(),
    )
