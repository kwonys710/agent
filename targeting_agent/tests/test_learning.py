"""Feedback 축적 / Profile·가중치 학습 테스트 (Phase 10)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.analysis.profile_analyzer import bump_version, profile_to_mapping
from targeting_agent.analysis.scorer import TargetScorer
from targeting_agent.core.database import (
    enqueue_action,
    insert_candidate,
    record_interaction,
    reset_skipped_for_rescore,
    save_analysis,
    set_state,
    update_candidate_status,
)
from targeting_agent.core.models import (
    ActionType,
    ContentAnalysis,
    FeedbackType,
    MediaStatus,
    RawCandidate,
)
from targeting_agent.learning.feedback import (
    feedback_counts,
    record_action_feedback,
    record_feedback,
    record_for_target,
    resolve_target,
)
from targeting_agent.learning.profile_optimizer import (
    UPDATED_AT_KEY,
    apply_learning,
    collect_keyword_signals,
    load_weights_override,
    reset_learning,
    should_update,
    suggest_profile,
    suggest_weights,
)

WEIGHTS = {
    "content_similarity": 0.35,
    "creator_fit": 0.20,
    "activity": 0.15,
    "engagement": 0.15,
    "language_region": 0.10,
    "novelty": 0.05,
}


def _media_with_analysis(conn, media_id: str, hashtags: list[str], username: str = "tester") -> int:
    """해시태그와 분석 결과가 있는 후보를 만든다."""
    candidate = RawCandidate(
        media_id=media_id,
        permalink=f"https://www.instagram.com/reel/{media_id}/",
        username=username,
        caption=" ".join(f"#{t}" for t in hashtags),
        hashtags=list(hashtags),
        followers=4000,
    )
    media_pk = insert_candidate(conn, candidate)
    save_analysis(
        conn,
        media_pk,
        ContentAnalysis(topics=["직장생활"], keywords=list(hashtags), language="ko"),
    )
    conn.commit()
    return int(media_pk)


def _feedback(conn, media_pk: int, types: list[FeedbackType]) -> None:
    for feedback_type in types:
        record_feedback(conn, feedback_type, media_pk=media_pk, creator_id=1)
    conn.commit()


# --- Feedback 입력 -------------------------------------------------------
def test_record_and_count(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    record_feedback(conn, FeedbackType.GOOD_TARGET, media_pk=media_pk, creator_id=1)
    record_action_feedback(conn, action_type="LIKE", media_pk=media_pk, creator_id=1)
    record_action_feedback(conn, action_type="COMMENT", media_pk=media_pk, creator_id=1)
    assert record_action_feedback(conn, action_type="UNKNOWN", media_pk=media_pk, creator_id=1) is None

    assert feedback_counts(conn) == {"GOOD_TARGET": 1, "LIKED": 1, "COMMENTED": 1}


def test_resolve_target_variants(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    action_id = enqueue_action(
        conn, run_id="r1", media_pk=media_pk, creator_id=1, action_type=ActionType.LIKE,
        target_score=90.0, priority=90, executor="manual", dry_run=True,
    )
    conn.commit()

    by_media = resolve_target(conn, sample_candidate.media_id)
    assert by_media.media_pk == media_pk and by_media.action_id is None

    by_creator = resolve_target(conn, f"@{sample_candidate.username}")
    assert by_creator.creator_id == 1 and by_creator.media_pk is None

    by_action = resolve_target(conn, str(action_id))
    assert by_action.action_id == action_id and by_action.media_pk == media_pk

    for bad in ("", "@없는계정", "999999", "NO_SUCH_MEDIA"):
        with pytest.raises(ValueError):
            resolve_target(conn, bad)


def test_record_for_target_writes_note(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    conn.commit()
    target = resolve_target(conn, sample_candidate.media_id)
    record_for_target(conn, FeedbackType.NOT_MY_STYLE, target, note="내 색이 아님")
    conn.commit()

    row = conn.execute("SELECT * FROM feedback").fetchone()
    assert row["feedback_type"] == "NOT_MY_STYLE"
    assert row["note"] == "내 색이 아님"
    assert row["media_pk"] == media_pk


# --- 키워드 신호 ---------------------------------------------------------
def test_signals_use_hashtags_not_caption_tokens(conn) -> None:
    """캡션 토큰(조사/어미)이 아니라 해시태그를 학습 신호로 쓴다."""
    media_pk = _media_with_analysis(conn, "M1", ["지하철", "출근"])
    conn.execute(
        "UPDATE media_analysis SET payload = ? WHERE media_pk = ?",
        ('{"keywords": ["보는", "오늘도"], "topics": ["직장생활"]}', media_pk),
    )
    _feedback(conn, media_pk, [FeedbackType.GOOD_TARGET])

    signals, samples = collect_keyword_signals(conn)
    assert samples == 1
    assert set(signals) == {"지하철", "출근"}
    assert "보는" not in signals


def test_signals_track_distinct_media(conn) -> None:
    first = _media_with_analysis(conn, "M1", ["지하철"])
    second = _media_with_analysis(conn, "M2", ["지하철"], username="other")
    _feedback(conn, first, [FeedbackType.GOOD_TARGET, FeedbackType.RESPONDED])
    _feedback(conn, second, [FeedbackType.GOOD_TARGET])

    signals, _ = collect_keyword_signals(conn)
    assert signals["지하철"].media_count == 2
    assert signals["지하철"].occurrences == 3
    assert signals["지하철"].score == pytest.approx(1.0 + 1.5 + 1.0)


# --- Profile 제안 --------------------------------------------------------
def test_profile_requires_min_samples(conn, profile) -> None:
    media_pk = _media_with_analysis(conn, "M1", ["지하철"])
    _feedback(conn, media_pk, [FeedbackType.GOOD_TARGET])

    suggestion = suggest_profile(conn, profile, min_samples=10)
    assert suggestion.has_change is False
    assert "표본이 부족" in suggestion.rationale[0]


def test_profile_requires_multiple_media(conn, profile) -> None:
    """게시물 한 건에서만 나온 키워드로는 Profile을 바꾸지 않는다."""
    media_pk = _media_with_analysis(conn, "M1", ["지하철"])
    _feedback(
        conn,
        media_pk,
        [FeedbackType.GOOD_TARGET, FeedbackType.RESPONDED, FeedbackType.FOLLOWED],
    )
    suggestion = suggest_profile(conn, profile, min_samples=1, min_occurrences=2, min_media=2)
    assert suggestion.add_keywords == []


def test_profile_learns_repeated_hashtag(conn, profile) -> None:
    for index, name in enumerate(("M1", "M2")):
        media_pk = _media_with_analysis(conn, name, ["지하철"], username=f"user{index}")
        _feedback(conn, media_pk, [FeedbackType.GOOD_TARGET, FeedbackType.RESPONDED])

    suggestion = suggest_profile(conn, profile, min_samples=1, min_occurrences=2, min_media=2)
    assert suggestion.add_keywords == ["지하철"]
    assert suggestion.has_change is True


def test_profile_keyword_not_inverted_by_feedback(conn, profile) -> None:
    """부정 신호가 있어도 운영자가 선언한 Profile 키워드를 회피로 바꾸지 않는다."""
    for index, name in enumerate(("M1", "M2")):
        media_pk = _media_with_analysis(conn, name, ["카페"], username=f"user{index}")
        _feedback(conn, media_pk, [FeedbackType.NOT_MY_STYLE, FeedbackType.BAD_TARGET])

    suggestion = suggest_profile(conn, profile, min_samples=1, min_occurrences=2, min_media=2)
    assert suggestion.add_avoid == []
    assert any("카페" in conflict for conflict in suggestion.conflicts)


def test_inflected_form_of_profile_keyword_also_protected(conn, profile) -> None:
    """'카페에서'처럼 Profile 키워드를 포함한 변형도 회피 목록에 넣지 않는다."""
    for index, name in enumerate(("M1", "M2")):
        media_pk = _media_with_analysis(conn, name, ["카페에서"], username=f"user{index}")
        _feedback(conn, media_pk, [FeedbackType.NOT_MY_STYLE, FeedbackType.BAD_TARGET])

    suggestion = suggest_profile(conn, profile, min_samples=1, min_occurrences=2, min_media=2)
    assert suggestion.add_avoid == []
    assert suggestion.conflicts


def test_profile_learns_avoid_for_new_keyword(conn, profile) -> None:
    for index, name in enumerate(("M1", "M2")):
        media_pk = _media_with_analysis(conn, name, ["밈챌린지"], username=f"user{index}")
        _feedback(conn, media_pk, [FeedbackType.NOT_MY_STYLE, FeedbackType.BAD_TARGET])

    suggestion = suggest_profile(conn, profile, min_samples=1, min_occurrences=2, min_media=2)
    assert suggestion.add_avoid == ["밈챌린지"]


# --- 가중치 --------------------------------------------------------------
def test_suggest_weights_requires_samples(conn) -> None:
    suggestion = suggest_weights(conn, WEIGHTS, min_samples=10)
    assert suggestion.has_change is False


def test_suggest_weights_adjusts_and_normalizes(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    for _ in range(8):
        record_feedback(conn, FeedbackType.GOOD_TARGET, media_pk=media_pk, creator_id=1)
    for _ in range(4):
        record_feedback(conn, FeedbackType.RESPONDED, media_pk=media_pk, creator_id=1)

    suggestion = suggest_weights(conn, WEIGHTS, min_samples=10)
    assert suggestion.has_change is True
    assert abs(sum(suggestion.suggested.values()) - 1.0) < 0.01
    assert all(v > 0 for v in suggestion.suggested.values())


def test_scorer_uses_learned_weights(config, profile) -> None:
    override = {**WEIGHTS, "content_similarity": 0.5, "novelty": 0.0}
    scorer = TargetScorer(config, profile, override)
    assert scorer.weights["content_similarity"] == 0.5
    assert scorer.weights_source == "learned"
    assert TargetScorer(config, profile).weights_source == "config"


# --- 적용 / 초기화 --------------------------------------------------------
def test_apply_learning_writes_override_and_bumps_version(conn, profile) -> None:
    for index, name in enumerate(("M1", "M2")):
        media_pk = _media_with_analysis(conn, name, ["지하철"], username=f"user{index}")
        _feedback(conn, media_pk, [FeedbackType.GOOD_TARGET, FeedbackType.RESPONDED])

    profile_suggestion = suggest_profile(conn, profile, min_samples=1, min_occurrences=2, min_media=2)
    weight_suggestion = suggest_weights(conn, WEIGHTS, min_samples=1)
    applied = apply_learning(conn, profile, profile_suggestion, weight_suggestion)

    assert applied["keywords"] == ["지하철"]
    assert applied["version"] == bump_version(profile.version)

    stored = load_weights_override(conn)
    assert stored is not None and abs(sum(stored.values()) - 1.0) < 0.01


def test_apply_learning_noop_without_changes(conn, profile) -> None:
    profile_suggestion = suggest_profile(conn, profile, min_samples=99)
    weight_suggestion = suggest_weights(conn, WEIGHTS, min_samples=99)
    applied = apply_learning(conn, profile, profile_suggestion, weight_suggestion)

    assert applied["keywords"] == [] and applied["weights"] is None
    assert load_weights_override(conn) is None


def test_reset_learning_removes_override(conn, profile) -> None:
    set_state(conn, "target_profile", "{}")
    set_state(conn, "scoring_weights", '{"content_similarity": 1.0}')
    set_state(conn, UPDATED_AT_KEY, "2026-01-01T00:00:00+00:00")
    conn.commit()

    reset_learning(conn)
    assert load_weights_override(conn) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM app_state WHERE key IN ('target_profile','scoring_weights')"
    ).fetchone()[0] == 0


def test_should_update_respects_interval(conn) -> None:
    assert should_update(conn, 7) is True  # 기록 없음 → 즉시 가능

    now = datetime.now(timezone.utc)
    set_state(conn, UPDATED_AT_KEY, (now - timedelta(days=2)).isoformat())
    conn.commit()
    assert should_update(conn, 7, now) is False
    assert should_update(conn, 1, now) is True
    assert should_update(conn, 0, now) is True  # 주기 0 = 항상 허용


def test_profile_to_mapping_roundtrip(profile) -> None:
    from targeting_agent.analysis.profile_analyzer import profile_from_mapping

    data = profile_to_mapping(profile)
    restored = profile_from_mapping(data)
    assert restored.all_keywords == profile.all_keywords
    assert restored.avoid_keywords == profile.avoid_keywords
    assert restored.version == profile.version


# --- 재채점 --------------------------------------------------------------
def test_rescore_resets_only_skipped_without_interaction(conn) -> None:
    skipped = _media_with_analysis(conn, "M1", ["지하철"])
    blocked = _media_with_analysis(conn, "M2", ["광고"], username="ad_user")
    done = _media_with_analysis(conn, "M3", ["퇴근"], username="done_user")

    update_candidate_status(conn, skipped, MediaStatus.SKIPPED, "low_score")
    update_candidate_status(conn, blocked, MediaStatus.BLOCKED, "ad_content")
    update_candidate_status(conn, done, MediaStatus.SKIPPED, "low_score")
    record_interaction(
        conn, media_pk=done, creator_id=3, action_id=None, action_type=ActionType.LIKE,
        executor="manual", dry_run=True, success=True,
    )
    conn.commit()

    assert reset_skipped_for_rescore(conn) == 1
    statuses = {
        row["media_pk"]: row["status"]
        for row in conn.execute("SELECT media_pk, status FROM candidate_media")
    }
    assert statuses[skipped] == MediaStatus.NEW.value
    assert statuses[blocked] == MediaStatus.BLOCKED.value   # 광고/민감은 되살리지 않는다
    assert statuses[done] == MediaStatus.SKIPPED.value      # 이미 Interaction 있음
