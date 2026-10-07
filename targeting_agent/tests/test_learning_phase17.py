"""Phase 17 — Learning & Operational Stabilization 테스트.

학습 계산은 결정론적이며 Claude를 호출하지 않는다(FakeRunner조차 필요 없다).
"""
from __future__ import annotations

import json

import pytest

from targeting_agent.analysis.scorer import TargetScorer
from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    bump_stat,
    enqueue_action,
    insert_candidate,
    save_analysis,
    save_comment_draft,
    today_str,
    update_candidate_status,
    utc_now,
)
from targeting_agent.core.models import (
    ActionType,
    ContentAnalysis,
    FeedbackType,
    MediaStatus,
    RawCandidate,
)
from targeting_agent.learning.comment_stats import collect_comment_preference
from targeting_agent.learning.feedback import record_feedback
from targeting_agent.learning.metrics import (
    collect_health_metrics,
    collect_quality_metrics,
    recommend_thresholds,
)
from targeting_agent.learning.profiles import (
    SOURCE_LEARNING,
    create_version,
    get_active_profile,
    list_versions,
    rollback,
)
from targeting_agent.learning.topics import (
    STATUS_NOT_ENOUGH_DATA,
    STATUS_OK,
    collect_signals,
    plan_learning,
)

TOPIC = "직장생활"


def _learning_config(config: Config, **overrides) -> Config:
    learning = {
        **config.raw.get("learning", {}),
        "minimum_feedback_count": 3,
        "minimum_topic_samples": 1,
        "max_delta_per_learning_run": 0.05,
        "min_topic_weight": 0.5,
        "max_topic_weight": 1.5,
        **overrides,
    }
    return Config(
        raw={**config.raw, "learning": learning}, path=config.path, base_dir=config.base_dir
    )


def _candidate(conn, media_id="M1", topics=(TOPIC,), score=88.0, username=None) -> int:
    media_pk = insert_candidate(
        conn,
        RawCandidate(
            media_id=media_id,
            permalink=f"https://www.instagram.com/reel/{media_id}/",
            username=username or f"creator_{media_id.lower()}",
            caption="퇴근 후 저녁 #퇴근",
            hashtags=["퇴근"],
            followers=5000,
        ),
    )
    save_analysis(
        conn,
        media_pk,
        ContentAnalysis(topics=list(topics), keywords=["퇴근"], analyzer="claude_code"),
        target_score=score,
    )
    update_candidate_status(conn, media_pk, MediaStatus.SCORED, "test", score)
    conn.commit()
    return int(media_pk)


def _feedback(conn, media_pk: int, feedback: FeedbackType, times: int = 1) -> None:
    for _ in range(times):
        record_feedback(conn, feedback, media_pk=media_pk, creator_id=1)
    conn.commit()


def _approve(conn, media_pk: int) -> None:
    action_id = enqueue_action(
        conn, run_id="r", media_pk=media_pk, creator_id=1, action_type=ActionType.LIKE,
        target_score=90.0, priority=90, executor="manual", dry_run=True,
    )
    conn.execute(
        "UPDATE action_queue SET approved_at = ? WHERE action_id = ?", (utc_now(), action_id)
    )
    conn.commit()


# --- 1~6. 신호 방향 -----------------------------------------------------------
def test_not_enough_feedback_keeps_profile(conn, config) -> None:
    _feedback(conn, _candidate(conn), FeedbackType.GOOD_TARGET)
    plan = plan_learning(conn, _learning_config(config, minimum_feedback_count=10), {})

    assert plan.status == STATUS_NOT_ENOUGH_DATA
    assert plan.changes == [] and plan.new_weights == {}


@pytest.mark.parametrize(
    "feedback,direction",
    [
        (FeedbackType.GOOD_TARGET, 1),
        (FeedbackType.RESPONDED, 1),
        (FeedbackType.FOLLOWED, 1),
        (FeedbackType.NOT_MY_STYLE, -1),
    ],
)
def test_feedback_moves_topic_weight(conn, config, feedback, direction) -> None:
    for index in range(3):
        _feedback(conn, _candidate(conn, f"M{index}"), feedback)

    plan = plan_learning(conn, _learning_config(config), {TOPIC: 1.0})
    change = next(c for c in plan.changes if c.topic == TOPIC)

    assert plan.status == STATUS_OK
    assert (change.delta > 0) if direction > 0 else (change.delta < 0)


def test_skip_is_weak_negative(conn, config) -> None:
    for index in range(3):
        media_pk = _candidate(conn, f"S{index}")
        update_candidate_status(conn, media_pk, MediaStatus.SKIPPED, "dashboard_skip")
        _feedback(conn, media_pk, FeedbackType.NOT_MY_STYLE)
    conn.commit()

    signals, _ = collect_signals(
        conn, {"NOT_MY_STYLE": -1.2, "SKIPPED": -0.3}
    )
    sources = signals[TOPIC].sources
    assert sources["SKIPPED"] == 3
    assert signals[TOPIC].average < 0


def test_approval_is_positive_signal(conn, config) -> None:
    for index in range(3):
        media_pk = _candidate(conn, f"A{index}")
        _approve(conn, media_pk)
        _feedback(conn, media_pk, FeedbackType.GOOD_TARGET)

    signals, _ = collect_signals(conn, {"GOOD_TARGET": 1.0, "APPROVED": 0.5})
    assert signals[TOPIC].sources["APPROVED"] == 3


# --- 7~9. 변화 제한 -------------------------------------------------------------
def test_max_delta_limits_change(conn, config) -> None:
    for index in range(10):
        _feedback(conn, _candidate(conn, f"D{index}"), FeedbackType.GOOD_TARGET, times=3)

    plan = plan_learning(conn, _learning_config(config, max_delta_per_learning_run=0.05), {TOPIC: 1.0})
    change = next(c for c in plan.changes if c.topic == TOPIC)

    assert abs(change.delta) <= 0.05 + 1e-9


def test_weight_bounds_respected(conn, config) -> None:
    for index in range(5):
        _feedback(conn, _candidate(conn, f"B{index}"), FeedbackType.GOOD_TARGET)

    plan = plan_learning(conn, _learning_config(config, max_topic_weight=1.02), {TOPIC: 1.0})
    assert plan.new_weights[TOPIC] <= 1.02

    plan_low = plan_learning(conn, _learning_config(config), {TOPIC: 0.5})
    assert plan_low.new_weights[TOPIC] >= 0.5


def test_topic_sample_threshold(conn, config) -> None:
    _feedback(conn, _candidate(conn, "T1", topics=("희귀토픽",)), FeedbackType.GOOD_TARGET)
    for index in range(3):
        _feedback(conn, _candidate(conn, f"C{index}"), FeedbackType.GOOD_TARGET)

    plan = plan_learning(conn, _learning_config(config, minimum_topic_samples=3), {})
    topics = {c.topic for c in plan.changes}
    assert TOPIC in topics and "희귀토픽" not in topics


# --- 10~13. Profile 버전 --------------------------------------------------------
def test_active_profile_created_on_demand(conn) -> None:
    active = get_active_profile(conn)
    assert active.version == "v1" and active.topic_weights == {}
    assert get_active_profile(conn).version == "v1"     # 중복 생성하지 않는다


def test_apply_creates_new_version_and_keeps_old(conn) -> None:
    base = get_active_profile(conn)
    created = create_version(
        conn, {TOPIC: 1.05}, source=SOURCE_LEARNING, reason="테스트", previous_version=base.version
    )

    assert created.version == "v2"
    assert get_active_profile(conn).version == "v2"
    versions = {p.version for p in list_versions(conn)}
    assert versions == {"v1", "v2"}                     # 기존 버전 보존
    stored = conn.execute(
        "SELECT topic_weights FROM target_profiles WHERE version = 'v1'"
    ).fetchone()[0]
    assert json.loads(stored) == {}                      # v1은 수정되지 않았다


def test_rollback_restores_previous_version(conn) -> None:
    base = get_active_profile(conn)
    create_version(conn, {TOPIC: 1.05}, source=SOURCE_LEARNING, previous_version=base.version)

    ok, message = rollback(conn)
    assert ok and "v2 → v1" in message
    assert get_active_profile(conn).version == "v1"
    assert len(list_versions(conn)) == 2                # 기록은 지우지 않는다


def test_rollback_without_history_fails(conn) -> None:
    get_active_profile(conn)
    ok, message = rollback(conn)
    assert ok is False and "되돌릴" in message


# --- 14~16. Scorer 연결 ---------------------------------------------------------
def test_scorer_applies_topic_weight_to_content_similarity_only(config, profile) -> None:
    analysis = ContentAnalysis(
        topics=[TOPIC], keywords=["퇴근"], language="ko", relevance_score=0.8
    )
    media = {"caption": "퇴근 후 저녁", "followers": 5000, "like_count": 100,
             "comment_count": 5, "is_private": 0}

    base = TargetScorer(config, profile).score(media, analysis)
    boosted = TargetScorer(
        config, profile, topic_weights={TOPIC: 1.1}, profile_version="v2"
    ).score(media, analysis)

    assert boosted.components["content_similarity"] > base.components["content_similarity"]
    for name in ("creator_fit", "activity", "engagement", "language_region", "novelty"):
        assert boosted.components[name] == base.components[name]   # 공식은 그대로
    assert boosted.profile_version == "v2"
    assert any(note.startswith("topic_weight") for note in boosted.notes)


def test_score_breakdown_records_profile_version(conn, config, profile) -> None:
    media_pk = _candidate(conn)
    analysis = ContentAnalysis(topics=[TOPIC], relevance_score=0.8)
    scorer = TargetScorer(config, profile, topic_weights={}, profile_version="v3")
    breakdown = scorer.score({"followers": 3000}, analysis)

    save_analysis(conn, media_pk, analysis, target_score=breakdown.total,
                  score_breakdown=breakdown.to_dict())
    conn.commit()

    stored = json.loads(
        conn.execute(
            "SELECT score_breakdown FROM media_analysis WHERE media_pk = ? ORDER BY analysis_id DESC LIMIT 1",
            (media_pk,),
        ).fetchone()[0]
    )
    assert stored["profile_version"] == "v3"


def test_existing_candidate_scores_not_recalculated(conn, config) -> None:
    """새 Profile을 적용해도 과거 후보 점수를 건드리지 않는다."""
    media_pk = _candidate(conn, score=88.0)
    base = get_active_profile(conn)
    create_version(conn, {TOPIC: 1.2}, source=SOURCE_LEARNING, previous_version=base.version)

    score = conn.execute(
        "SELECT target_score FROM candidate_media WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0]
    assert score == 88.0


# --- 17~18. 댓글 선호 ------------------------------------------------------------
def test_comment_preference_stats(conn, config) -> None:
    media_pk = _candidate(conn)
    save_comment_draft(
        conn, media_pk, "퇴근 후 이런 한 끼가 진짜 좋죠", "원본", language="ko", quality_ok=True,
        quality_reason="", similarity_max=0.0, status="CANDIDATE",
        generator="claude_code", generator_version="1",
    )
    save_comment_draft(
        conn, media_pk, "퇴근하고 이런 한 끼가 최고죠 😂", "수정", language="ko", quality_ok=True,
        quality_reason="operator_edit", similarity_max=0.0, status="SELECTED",
        generator="operator", generator_version="1",
    )
    _feedback(conn, media_pk, FeedbackType.GOOD_COMMENT)

    preference = collect_comment_preference(conn)
    assert preference.selected_count == 1 and preference.edited_count == 1
    assert preference.edit_rate == 1.0
    assert preference.emoji_added == 1
    assert preference.good_comment == 1

    edit = preference.edits[0]
    assert edit.original.startswith("퇴근 후")
    assert edit.emoji_delta == 1 and 0 < edit.similarity < 1


# --- 19~24. 품질·운영 지표 --------------------------------------------------------
def test_quality_metrics_rates(conn, config) -> None:
    approved_pk = _candidate(conn, "Q1", score=92.0)
    _approve(conn, approved_pk)
    update_candidate_status(conn, approved_pk, MediaStatus.QUEUED, "approved", 92.0)
    skipped_pk = _candidate(conn, "Q2", score=75.0)
    update_candidate_status(conn, skipped_pk, MediaStatus.SKIPPED, "skip", 75.0)
    _feedback(conn, approved_pk, FeedbackType.GOOD_TARGET)
    _feedback(conn, skipped_pk, FeedbackType.NOT_MY_STYLE)

    metrics = collect_quality_metrics(conn)
    assert metrics.reviewed == 2 and metrics.approved == 1
    assert metrics.approval_rate == 0.5
    assert metrics.good_target_rate == 0.5 and metrics.not_my_style_rate == 0.5
    assert metrics.claude_approval_rate is not None
    assert metrics.score_bands["90+"]["approved"] == 1
    assert metrics.score_bands["70-79"]["rate"] == 0.0


def test_health_metrics_and_warnings(conn, config) -> None:
    date = today_str(9)
    bump_stat(conn, date, "claude_requests", 10)
    bump_stat(conn, date, "claude_cache_hits", 1)
    bump_stat(conn, date, "claude_fallbacks", 6)
    conn.commit()

    health = collect_health_metrics(conn, config)
    assert health.claude_calls == 10 and health.cache_hits == 1
    assert health.fallback_rate is not None and health.fallback_rate > 0.3
    assert any("Fallback" in w for w in health.warnings)
    assert any("Cache Hit" in w for w in health.warnings)


def test_health_warnings_suppressed_with_small_samples(conn, config) -> None:
    bump_stat(conn, today_str(9), "claude_requests", 2)
    bump_stat(conn, today_str(9), "claude_fallbacks", 2)
    conn.commit()

    health = collect_health_metrics(conn, config)
    assert health.warnings == []        # 표본이 적으면 경고하지 않는다


def test_threshold_recommendation_does_not_change_config(conn, config) -> None:
    for index in range(6):
        media_pk = _candidate(conn, f"R{index}", score=75.0)
        update_candidate_status(conn, media_pk, MediaStatus.SKIPPED, "skip", 75.0)

    metrics = collect_quality_metrics(conn)
    recommendations = recommend_thresholds(config, metrics)

    assert any("검토" in text or "추천" in text for text in recommendations)
    assert any("자동으로 변경되지 않습니다" in text for text in recommendations)
    assert float(config.get("scoring.minimum_target_score")) == 70   # 설정은 그대로


# --- 25~27. 표시 / Claude 미사용 ---------------------------------------------------
def test_dashboard_learning_section(conn, config) -> None:
    from targeting_agent.dashboard.app import Page, render

    base = get_active_profile(conn)
    create_version(conn, {TOPIC: 1.05}, source=SOURCE_LEARNING, previous_version=base.version)
    html = render(conn, config, Page(tab="stats"), token="t")

    assert "Learning" in html and "v2" in html
    assert "직장생활 1.05" in html
    assert "--learn-apply" in html


def test_daily_summary_learning_section(conn, config, tmp_path) -> None:
    from targeting_agent.scheduler.summary import build_summary_data, render_summary_html

    get_active_profile(conn)
    data = build_summary_data(conn, config, today_str(9))
    html = render_summary_html(data)

    assert "Learning" in html and "Active Profile" in html
    assert "자동 실행되지 않는다" in html


def test_learning_never_calls_claude(conn, config, monkeypatch) -> None:
    """학습 계산 경로에서 Claude CLI를 호출하지 않는다."""
    import subprocess

    def fail(*args, **kwargs):  # pragma: no cover - 호출되면 테스트 실패
        raise AssertionError("학습 중 subprocess 호출이 발생했습니다")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)

    for index in range(3):
        _feedback(conn, _candidate(conn, f"N{index}"), FeedbackType.GOOD_TARGET)

    plan = plan_learning(conn, _learning_config(config), {})
    collect_quality_metrics(conn)
    collect_health_metrics(conn, config)
    collect_comment_preference(conn)

    assert plan.status == STATUS_OK
