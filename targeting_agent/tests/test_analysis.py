"""분석 / 유사도 / Target Score 테스트."""
from __future__ import annotations

from targeting_agent.analysis.content_analyzer import ContentAnalyzer, HeuristicAnalyzer
from targeting_agent.analysis.profile_analyzer import creator_fit
from targeting_agent.analysis.scorer import (
    TargetScorer,
    activity_score,
    disqualify_reason,
    engagement_score,
    language_score,
)
from targeting_agent.analysis.similarity import (
    detect_language,
    profile_similarity,
    text_similarity,
)
from targeting_agent.core.database import insert_candidate

ON_PROFILE = {
    "media_pk": 1,
    "caption": "퇴근 후 회사 근처 카페에서 마무리 #직장인 #퇴근후 #카페",
    "hashtags": '["직장인", "퇴근후", "카페"]',
    "followers": 5000,
    "like_count": 400,
    "comment_count": 25,
    "posted_at": "2026-09-20",
    "is_private": 0,
}
OFF_PROFILE = {
    "media_pk": 2,
    "caption": "Crypto trading signals, join now",
    "hashtags": '["coin", "trading"]',
    "followers": 900000,
    "like_count": 10,
    "comment_count": 0,
    "posted_at": "2020-01-01",
    "is_private": 0,
}


def test_detect_language() -> None:
    assert detect_language("오늘 퇴근길") == "ko"
    assert detect_language("morning routine") == "en"
    assert detect_language("") == "unknown"


def test_text_similarity_range() -> None:
    assert text_similarity("퇴근길 브이로그", "퇴근길 브이로그") == 1.0
    assert text_similarity("퇴근길 브이로그 좋네요", "퇴근길 브이로그 좋아요") > 0.6
    assert text_similarity("카페 분위기 좋네요", "코딩 테스트 준비중") < 0.4


def test_profile_similarity_prefers_on_profile(profile) -> None:
    on = profile_similarity(
        ["직장인", "퇴근후", "카페"], ["직장생활"], ON_PROFILE["caption"], profile.all_keywords, profile.topics
    )
    off = profile_similarity(
        ["coin", "trading"], ["기타"], OFF_PROFILE["caption"], profile.all_keywords, profile.topics
    )
    assert on > off
    assert 0.0 <= off < on <= 1.0


def test_heuristic_analyzer_flags(profile) -> None:
    analyzer = HeuristicAnalyzer(profile)
    analysis = analyzer.analyze(ON_PROFILE, ["직장인", "퇴근후", "카페"])
    assert analysis.language == "ko"
    assert "직장생활" in analysis.topics
    assert analysis.is_ad is False

    ad = analyzer.analyze({"caption": "신상 협찬 받았어요 #광고"}, ["광고"])
    assert ad.is_ad is True


def test_score_components() -> None:
    assert activity_score(None) == 0.5
    assert activity_score("2026-09-21") > activity_score("2026-01-01")
    assert engagement_score(500, 20, 5000) > engagement_score(10, 0, 5000)
    assert engagement_score(1, 0, 0) == 0.5          # 정보 없음 -> 중립
    assert language_score("ko", ["ko"]) == 1.0
    assert language_score("en", ["ko"]) == 0.0
    assert creator_fit(5000, min_followers=100, max_followers=30000, is_private=False) > 0.7
    assert creator_fit(5000, min_followers=100, max_followers=30000, is_private=True) == 0.0


def test_target_scorer_ranks_on_profile_higher(config, profile) -> None:
    scorer = TargetScorer(config, profile)
    analyzer = HeuristicAnalyzer(profile)

    on_score = scorer.score(ON_PROFILE, analyzer.analyze(ON_PROFILE, ["직장인", "퇴근후", "카페"]))
    off_score = scorer.score(OFF_PROFILE, analyzer.analyze(OFF_PROFILE, ["coin", "trading"]))

    assert 0 <= off_score.total < on_score.total <= 100
    assert on_score.total >= float(config.get("scoring.minimum_target_score"))
    assert set(on_score.components) == set(config.section("scoring.weights"))


def test_disqualify_ad_and_sensitive(config, profile) -> None:
    scorer = TargetScorer(config, profile)
    analyzer = HeuristicAnalyzer(profile)
    media = {"caption": "협찬 받은 제품 소개 #광고", "followers": 3000, "is_private": 0}
    analysis = analyzer.analyze(media, ["광고"])
    breakdown = scorer.score(media, analysis)
    assert disqualify_reason(config, media, analysis, breakdown) == "ad_content"


def test_analysis_cache_reuse(config, profile, conn, sample_candidate) -> None:
    """두 번째 호출은 DB 캐시를 재사용해야 한다(AI 호출 비용 절감)."""
    media_pk = insert_candidate(conn, sample_candidate)
    conn.commit()
    row = dict(conn.execute("SELECT * FROM candidate_media WHERE media_pk = ?", (media_pk,)).fetchone())

    analyzer = ContentAnalyzer(config, profile)
    first, cached_first = analyzer.analyze_media(conn, row)
    second, cached_second = analyzer.analyze_media(conn, row)

    assert cached_first is False and cached_second is True
    assert first.topics == second.topics
    assert conn.execute("SELECT COUNT(*) FROM media_analysis").fetchone()[0] == 1
