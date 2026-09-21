"""Feedback 축적 / Weight 조정 제안 테스트."""
from __future__ import annotations

from targeting_agent.core.database import insert_candidate
from targeting_agent.core.models import FeedbackType
from targeting_agent.learning.feedback import feedback_counts, record_action_feedback, record_feedback
from targeting_agent.learning.profile_optimizer import suggest_weights

WEIGHTS = {
    "content_similarity": 0.35,
    "creator_fit": 0.20,
    "activity": 0.15,
    "engagement": 0.15,
    "language_region": 0.10,
    "novelty": 0.05,
}


def test_record_and_count(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    record_feedback(conn, FeedbackType.GOOD_TARGET, media_pk=media_pk, creator_id=1)
    record_action_feedback(conn, action_type="LIKE", media_pk=media_pk, creator_id=1)
    record_action_feedback(conn, action_type="COMMENT", media_pk=media_pk, creator_id=1)
    assert record_action_feedback(conn, action_type="UNKNOWN", media_pk=media_pk, creator_id=1) is None

    counts = feedback_counts(conn)
    assert counts == {"GOOD_TARGET": 1, "LIKED": 1, "COMMENTED": 1}


def test_suggest_weights_requires_samples(conn) -> None:
    suggestion = suggest_weights(conn, WEIGHTS, min_samples=10)
    assert suggestion.has_change is False
    assert "표본이 부족" in suggestion.rationale[0]


def test_suggest_weights_adjusts_and_normalizes(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    for _ in range(8):
        record_feedback(conn, FeedbackType.GOOD_TARGET, media_pk=media_pk, creator_id=1)
    for _ in range(4):
        record_feedback(conn, FeedbackType.RESPONDED, media_pk=media_pk, creator_id=1)

    suggestion = suggest_weights(conn, WEIGHTS, min_samples=10)
    assert suggestion.has_change is True
    assert suggestion.sample_size == 12
    assert abs(sum(suggestion.suggested.values()) - 1.0) < 0.01
    assert suggestion.suggested["content_similarity"] > 0
    assert all(v > 0 for v in suggestion.suggested.values())
