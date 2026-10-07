"""댓글 생성기.

원칙:
- 게시물 맥락(분석 키워드/토픽)에 기반한 짧은 한국어 댓글
- Generic/홍보/과한 친밀감 표현 금지
- 게시물당 후보 N개(기본 3개) 생성
- 품질 필터 + 중복 필터를 통과할 때까지 재생성(최대 max_generation_attempts)

댓글 출처:
- Claude Code가 분석 단계에서 함께 만든 후보(있으면 우선 사용, 추가 호출 없음)
- 템플릿 기반 생성(항상 사용 가능한 fallback)

어느 쪽이든 품질/중복 필터를 동일하게 통과해야 한다.
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Optional, Sequence

from ..analysis.profile_analyzer import TargetProfile
from ..analysis.similarity import normalize_text
from ..core.config import Config
from ..core.logger import get_logger
from ..core.models import CommentCandidate, ContentAnalysis, DraftStatus
from .duplicate_filter import CommentDuplicateFilter
from .quality_filter import CommentQualityFilter

logger = get_logger("comments.generator")

# 토픽별 문장 틀. {term}에는 게시물에서 뽑은 맥락 단어가 들어간다.
TOPIC_TEMPLATES: dict[str, tuple[str, ...]] = {
    "직장생활": (
        "{term} 분위기 너무 현실적이라 공감되네요",
        "{term} 장면에서 제 하루가 겹쳐 보이네요",
        "{term} 리듬이 딱 이렇죠, 오늘도 고생하셨어요",
        "{term} 담는 시선이 담백해서 좋아요",
        "{term} 풍경 저도 매일 보는 장면이라 반갑네요",
        "{term} 기록해두니 하루가 달라 보이네요",
        "{term} 이런 장면이 제일 현실적이죠",
    ),
    "주말일상": (
        "{term} 여유가 화면에서 그대로 느껴지네요",
        "{term} 이런 속도가 제일 좋더라고요",
        "{term} 보니까 이번 주 계획이 생기네요",
        "{term} 분위기 차분해서 계속 보게 되네요",
        "{term} 시간만큼은 천천히 흘렀으면 좋겠어요",
        "{term} 공기까지 담긴 느낌이네요",
        "{term} 이렇게 보내는 하루 참 좋네요",
    ),
    "일상브이로그": (
        "{term} 소소한 장면이 제일 오래 남더라고요",
        "{term} 기록이 정갈해서 보기 편했어요",
        "{term} 이런 하루가 쌓이는 느낌 좋네요",
        "{term} 편집 호흡이 편안하네요",
        "{term} 담아두면 나중에 더 좋더라고요",
        "{term} 한 컷 한 컷이 차분하네요",
        "{term} 장면 고르신 감각이 좋네요",
    ),
    "기타": (
        "{term} 장면 구성이 깔끔하네요",
        "{term} 분위기가 오래 남네요",
        "{term} 색감이 차분해서 좋아요",
        "{term} 호흡이 편해서 끝까지 봤어요",
        "{term} 담는 방식이 마음에 드네요",
    ),
}

CAFE_FOOD_TEMPLATES = (
    "{term} 분위기 좋아 보여서 저장해둡니다",
    "{term} 여기 조용해 보여서 가보고 싶네요",
    "{term} 사진보다 영상이 훨씬 잘 담기네요",
    "{term} 자리 잡고 한참 있고 싶어지네요",
    "{term} 이런 곳 찾으면 하루가 든든하죠",
    "{term} 공간 분위기가 차분해서 좋네요",
)
CAFE_FOOD_HINTS = ("카페", "맛집", "브런치", "커피", "디저트", "음식")

SUFFIXES = ("", " :)", " 👏", " 👍")


def _pick_terms(analysis: ContentAnalysis, profile: TargetProfile, limit: int = 4) -> list[str]:
    """댓글에 넣을 맥락 단어를 고른다(Profile 키워드와 겹치는 것 우선)."""
    profile_keywords = {k.lower() for k in profile.all_keywords}
    preferred: list[str] = []
    others: list[str] = []
    for keyword in analysis.keywords:
        word = str(keyword).strip()
        if len(word) < 2 or len(word) > 12 or word.isdigit():
            continue
        target = preferred if word.lower() in profile_keywords else others
        if word not in target:
            target.append(word)
    terms = preferred + others
    if not terms:
        terms = [t for t in analysis.topics if t != "기타"] or ["일상"]
    return terms[:limit]


def _templates_for_term(term: str, analysis: ContentAnalysis, profile: TargetProfile) -> tuple[str, ...]:
    """맥락 단어와 어울리는 문장 틀만 고른다.

    단어와 틀이 따로 놀면 어색한 댓글이 나오므로(예: '직장인 ... 주말이 제일 좋더라고요'),
    단어가 속한 Profile 그룹을 먼저 판정한 뒤 해당 그룹의 틀만 사용한다.
    """
    lowered = term.lower()

    def in_group(group: tuple[str, ...]) -> bool:
        return any(lowered == k.lower() or k.lower() in lowered for k in group)

    if any(hint in lowered for hint in CAFE_FOOD_HINTS):
        return CAFE_FOOD_TEMPLATES
    if in_group(profile.weekday_keywords):
        return TOPIC_TEMPLATES["직장생활"]
    if in_group(profile.weekend_keywords):
        return TOPIC_TEMPLATES["주말일상"]
    if in_group(profile.shared_keywords):
        return TOPIC_TEMPLATES["일상브이로그"]

    # Profile 어느 그룹에도 없는 단어는 게시물 토픽을 따른다.
    for topic in analysis.topics:
        if topic in TOPIC_TEMPLATES:
            return TOPIC_TEMPLATES[topic]
    return TOPIC_TEMPLATES["기타"]


class TemplateCommentGenerator:
    """맥락 슬롯 기반 생성기(기본값)."""

    name = "template"
    version = "1"

    def __init__(self, profile: TargetProfile, allow_emoji: bool = True, seed: Optional[int] = None) -> None:
        self.profile = profile
        self.allow_emoji = allow_emoji
        self._random = random.Random(seed)

    def propose(
        self, media_row: Mapping[str, Any], analysis: ContentAnalysis, count: int
    ) -> list[str]:
        """서로 다른 문장 틀 × 맥락 단어 조합으로 후보 문장을 만든다."""
        terms = _pick_terms(analysis, self.profile)
        suffixes = SUFFIXES if self.allow_emoji else ("",)

        combos = [
            (template, term)
            for term in terms
            for template in _templates_for_term(term, analysis, self.profile)
        ]
        self._random.shuffle(combos)

        proposals: list[str] = []
        for index, (template, term) in enumerate(combos):
            if term in template.replace("{term}", ""):
                continue  # 같은 단어가 문장에서 반복되는 조합은 제외
            suffix = suffixes[index % len(suffixes)]
            text = normalize_text(template.format(term=term) + suffix)
            if text not in proposals:
                proposals.append(text)
            if len(proposals) >= count:
                break
        return proposals


class ClaudeCandidateGenerator:
    """Claude Code가 만든 댓글 후보를 제공하는 생성기(Phase 13).

    새로 호출하지 않는다. 분석 단계에서 이미 받아온 후보를 쓰고,
    모자라면 CommentGenerator가 템플릿으로 채운다.
    """

    name = "claude_code"
    version = "1"

    def __init__(self, candidates: Sequence[str]) -> None:
        self._candidates = [normalize_text(str(c)) for c in candidates if str(c).strip()]

    def propose(
        self, media_row: Mapping[str, Any], analysis: ContentAnalysis, count: int
    ) -> list[str]:
        return self._candidates[:count]


class CommentGenerator:
    """품질/중복 필터를 적용해 사용 가능한 댓글 후보를 만드는 Facade."""

    def __init__(self, config: Config, profile: TargetProfile, seed: Optional[int] = None) -> None:
        self.config = config
        self.count = int(config.get("comments.candidates_per_post", 3))
        self.max_attempts = int(config.get("comments.max_generation_attempts", 4))
        self.quality = CommentQualityFilter(config)
        self.template = TemplateCommentGenerator(
            profile, allow_emoji=bool(config.get("comments.allow_emoji", True)), seed=seed
        )
        self._backend: Any = self.template

    @property
    def backend_name(self) -> str:
        return getattr(self._backend, "name", "template")

    def generate(
        self,
        media_row: Mapping[str, Any],
        analysis: ContentAnalysis,
        duplicate_filter: CommentDuplicateFilter,
        context_keywords: Optional[Sequence[str]] = None,
    ) -> list[CommentCandidate]:
        """품질/중복을 통과한 댓글 후보 목록(최대 candidates_per_post)."""
        context = list(context_keywords or analysis.keywords)
        accepted: list[CommentCandidate] = []
        rejected: list[CommentCandidate] = []
        seen: set[str] = set()

        backends: list[Any] = []
        if analysis.comment_candidates:
            # 분석 때 받아온 Claude 후보를 먼저 쓴다(추가 호출 없음).
            backends.append(ClaudeCandidateGenerator(analysis.comment_candidates))
        backends.append(self._backend)

        for backend in backends:
            self._collect(
                backend, media_row, analysis, duplicate_filter, context, accepted, rejected, seen
            )
            if len(accepted) >= self.count:
                break

        if not accepted:
            logger.warning(
                "media_id=%s 댓글 후보를 만들지 못했습니다(거절 %d건).",
                media_row.get("media_id"),
                len(rejected),
            )
        if accepted:
            accepted[0].status = DraftStatus.SELECTED
        return accepted + rejected

    def _collect(
        self,
        backend: Any,
        media_row: Mapping[str, Any],
        analysis: ContentAnalysis,
        duplicate_filter: CommentDuplicateFilter,
        context: Sequence[str],
        accepted: list[CommentCandidate],
        rejected: list[CommentCandidate],
        seen: set[str],
    ) -> None:
        """한 생성기에서 후보를 받아 품질/중복 필터를 적용한다."""
        backend_name = getattr(backend, "name", "template")
        backend_version = getattr(backend, "version", "1")
        for attempt in range(1, self.max_attempts + 1):
            need = self.count - len(accepted)
            if need <= 0:
                return
            # 재시도할수록 더 많은 후보를 받아 통과 확률을 높인다.
            proposals = backend.propose(media_row, analysis, need * attempt * 2)
            if not proposals:
                return
            for text in proposals:
                if text in seen:
                    continue
                seen.add(text)

                quality = self.quality.check(text, context)
                if not quality.ok:
                    rejected.append(
                        CommentCandidate(
                            text=text,
                            generator=backend_name,
                            generator_version=backend_version,
                            quality_ok=False,
                            quality_reason=quality.reason,
                            status=DraftStatus.REJECTED,
                        )
                    )
                    continue

                duplicate = duplicate_filter.check(text)
                if duplicate.is_duplicate:
                    rejected.append(
                        CommentCandidate(
                            text=text,
                            generator=backend_name,
                            generator_version=backend_version,
                            quality_ok=False,
                            quality_reason=f"duplicate({duplicate.similarity:.2f})",
                            similarity_max=duplicate.similarity,
                            status=DraftStatus.REJECTED,
                        )
                    )
                    continue

                duplicate_filter.remember(text)
                accepted.append(
                    CommentCandidate(
                        text=text,
                        generator=backend_name,
                        generator_version=backend_version,
                        quality_ok=True,
                        similarity_max=duplicate.similarity,
                        status=DraftStatus.CANDIDATE,
                    )
                )
                if len(accepted) >= self.count:
                    return
