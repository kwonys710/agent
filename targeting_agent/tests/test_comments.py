"""댓글 생성 / 품질 / 중복 필터 테스트."""
from __future__ import annotations

from targeting_agent.comments.duplicate_filter import CommentDuplicateFilter
from targeting_agent.comments.generator import CommentGenerator
from targeting_agent.comments.quality_filter import CommentQualityFilter
from targeting_agent.core.models import ContentAnalysis, DraftStatus

CONTEXT = ["직장인", "퇴근", "카페"]
MEDIA = {"media_pk": 1, "media_id": "M1", "caption": "퇴근 후 카페 #직장인 #퇴근 #카페"}
ANALYSIS = ContentAnalysis(
    topics=["직장생활"], keywords=CONTEXT, language="ko", summary="퇴근 후 카페"
)


def test_quality_rejects_generic_and_banned(config) -> None:
    checker = CommentQualityFilter(config)
    assert checker.check("영상 잘 봤어요", CONTEXT).ok is False
    assert checker.check("잘 보고 갑니다", CONTEXT).ok is False
    assert checker.check("맞팔해요", CONTEXT).ok is False


def test_quality_rejects_promo_and_links(config) -> None:
    checker = CommentQualityFilter(config)
    assert "promotional" in checker.check("팔로우하고 갈게요 퇴근", CONTEXT).reason
    assert checker.check("퇴근 영상 www.example.com", CONTEXT).reason == "contains_url"
    assert checker.check("퇴근 @someone 보세요", CONTEXT).reason == "contains_mention"
    assert checker.check("퇴근 #직장인", CONTEXT).reason == "contains_hashtag"


def test_quality_length_and_emoji_and_context(config) -> None:
    checker = CommentQualityFilter(config)
    assert checker.check("퇴근", CONTEXT).reason.startswith("too_short")
    assert checker.check("퇴근 " * 40, CONTEXT).reason.startswith("too_long")
    assert "too_many_emoji" in checker.check("퇴근 분위기 좋네요 👏👏👏", CONTEXT).reason
    # 맥락 키워드가 없으면 거절(Generic Comment 차단)
    assert checker.check("화면 구성이 아주 인상적이네요", CONTEXT).reason == "no_context_keyword"
    assert checker.check("퇴근 분위기가 차분해서 좋네요", CONTEXT).ok is True


def test_duplicate_filter_threshold(config) -> None:
    dup = CommentDuplicateFilter(config, corpus=["퇴근 분위기가 차분해서 좋네요"])
    assert dup.check("퇴근 분위기가 차분해서 좋네요").is_duplicate is True
    assert dup.check("카페 색감이 정말 편안하네요").is_duplicate is False

    dup.remember("카페 색감이 정말 편안하네요")
    assert dup.check("카페 색감이 정말 편안하네요").is_duplicate is True


def test_generator_produces_context_comments(config, profile) -> None:
    generator = CommentGenerator(config, profile, seed=42)
    dup = CommentDuplicateFilter(config, corpus=[])
    candidates = generator.generate(MEDIA, ANALYSIS, dup)

    accepted = [c for c in candidates if c.quality_ok]
    assert len(accepted) == int(config.get("comments.candidates_per_post"))
    assert accepted[0].status is DraftStatus.SELECTED
    assert len({c.text for c in accepted}) == len(accepted)     # 서로 다른 문장
    for candidate in accepted:
        assert any(keyword in candidate.text for keyword in CONTEXT)
        assert 5 <= len(candidate.text) <= 60
        assert "잘 봤" not in candidate.text


def test_generator_avoids_existing_comments(config, profile) -> None:
    """기존 댓글과 동일/유사한 문장은 채택되지 않는다."""
    generator = CommentGenerator(config, profile, seed=1)
    first = [c for c in generator.generate(MEDIA, ANALYSIS, CommentDuplicateFilter(config, [])) if c.quality_ok]
    corpus = [c.text for c in first]

    second_filter = CommentDuplicateFilter(config, corpus=corpus)
    second = [c for c in generator.generate(MEDIA, ANALYSIS, second_filter) if c.quality_ok]
    assert all(text not in corpus for text in (c.text for c in second))
