"""Phase 14 — Dashboard Action UI 테스트.

실제 Instagram 동작이나 Claude CLI를 호출하지 않는다.
UI(app.render)와 상태 변경(service)을 분리해 검증한다.
"""
from __future__ import annotations

import json

import pytest

from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    enqueue_action,
    insert_candidate,
    record_interaction,
    save_analysis,
    save_comment_draft,
    update_candidate_status,
)
from targeting_agent.core.exceptions import ConfigError
from targeting_agent.core.models import (
    ActionStatus,
    ActionType,
    ContentAnalysis,
    FeedbackType,
    MediaStatus,
    RawCandidate,
)
from targeting_agent.dashboard import queries
from targeting_agent.dashboard.app import Page, render, validate_host
from targeting_agent.dashboard.service import DashboardService

CLAUDE_ANALYSIS = ContentAnalysis(
    topics=["직장생활", "퇴근"],
    keywords=["퇴근", "직장인"],
    language="ko",
    tone="차분",
    summary="퇴근 후 저녁 준비",
    analyzer="claude_code",
    relevance_score=0.92,
    relevance_reason="직장인 퇴근 맥락이 겹친다",
    comment_candidates=["퇴근하고 이 메뉴는 못 참죠", "이런 한 끼가 진짜 행복이죠"],
)


def _candidate(conn, media_id="M1", username="creator_a", score=88.0, status=MediaStatus.SCORED,
               analysis=CLAUDE_ANALYSIS, drafts=("퇴근하고 이 메뉴는 못 참죠", "이런 한 끼가 진짜 행복이죠")):
    media_pk = insert_candidate(
        conn,
        RawCandidate(
            media_id=media_id,
            permalink=f"https://www.instagram.com/reel/{media_id}/",
            username=username,
            caption="퇴근 후 저녁 만들기 #퇴근 #직장인",
            hashtags=["퇴근", "직장인"],
            followers=5000,
        ),
    )
    if analysis is not None:
        save_analysis(
            conn, media_pk, analysis,
            similarity=0.9, target_score=score,
            score_breakdown={"components": {"content_similarity": 0.92, "creator_fit": 0.8},
                             "notes": ["similarity:claude"]},
        )
    update_candidate_status(conn, media_pk, status, "test", score)
    for text in drafts:
        save_comment_draft(
            conn, media_pk, text, text.lower(), language="ko", quality_ok=True,
            quality_reason="", similarity_max=0.0, status="CANDIDATE",
            generator="claude_code", generator_version="1",
        )
    conn.commit()
    return media_pk


def _service(conn, config) -> DashboardService:
    return DashboardService(conn, config)


def _drafts(conn, media_pk):
    return {
        row["text"]: row["status"]
        for row in conn.execute(
            "SELECT text, status FROM comment_drafts WHERE media_pk = ?", (media_pk,)
        )
    }


# --- 1~2. 바인딩 ------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_localhost_binding_allowed(host: str) -> None:
    assert validate_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "example.com", ""])
def test_external_binding_refused(host: str) -> None:
    with pytest.raises(ConfigError, match="127.0.0.1"):
        validate_host(host)


# --- 3~5. 조회 --------------------------------------------------------------
def test_candidate_list_sorted_by_score(conn, config) -> None:
    _candidate(conn, "LOW", "low_user", score=60.0)
    _candidate(conn, "HIGH", "high_user", score=95.0)

    rows = queries.list_candidates(conn)
    assert [r["media_id"] for r in rows] == ["HIGH", "LOW"]


def test_candidate_filters(conn, config) -> None:
    high = _candidate(conn, "H1", "claude_user", score=90.0)
    _candidate(conn, "H2", "heur_user", score=70.0,
               analysis=ContentAnalysis(topics=["기타"], analyzer="heuristic"))

    assert [r["media_pk"] for r in queries.list_candidates(conn, analyzer="claude_code")] == [high]
    assert len(queries.list_candidates(conn, min_score=80)) == 1
    assert queries.list_candidates(conn, source="없는소스") == []


def test_candidate_detail_includes_analysis_comments_and_history(conn, config) -> None:
    media_pk = _candidate(conn)
    record_interaction(
        conn, media_pk=media_pk, creator_id=1, action_id=None, action_type=ActionType.LIKE,
        executor="manual", dry_run=True, success=True,
    )
    conn.commit()

    detail = queries.candidate_detail(conn, media_pk)
    assert detail["analysis"]["relevance_score"] == 0.92
    assert detail["analysis"]["relevance_reason"]
    assert len(detail["comments"]) == 2
    assert len(detail["interactions"]) == 1
    assert detail["breakdown"]["components"]["content_similarity"] == 0.92


# --- 6~7. 댓글 선택/수정 -----------------------------------------------------
def test_select_comment_marks_single_selection(conn, config) -> None:
    media_pk = _candidate(conn)
    drafts = conn.execute(
        "SELECT draft_id, text FROM comment_drafts WHERE media_pk = ? ORDER BY draft_id", (media_pk,)
    ).fetchall()

    service = _service(conn, config)
    service.select_comment(media_pk, draft_id=int(drafts[0]["draft_id"]))
    service.select_comment(media_pk, draft_id=int(drafts[1]["draft_id"]))

    statuses = _drafts(conn, media_pk)
    assert statuses[drafts[1]["text"]] == "SELECTED"
    assert statuses[drafts[0]["text"]] == "CANDIDATE"   # 선택은 항상 1개


def test_edited_comment_kept_as_new_draft(conn, config) -> None:
    media_pk = _candidate(conn)
    service = _service(conn, config)

    draft_id = service.select_comment(media_pk, text="퇴근하고 먹는 이 한 끼가 최고죠")
    statuses = _drafts(conn, media_pk)

    assert statuses["퇴근하고 먹는 이 한 끼가 최고죠"] == "SELECTED"
    assert len(statuses) == 3                               # 원본 2개 보존
    generator = conn.execute(
        "SELECT generator FROM comment_drafts WHERE draft_id = ?", (draft_id,)
    ).fetchone()[0]
    assert generator == "operator"


# --- 8~11. 승인 -------------------------------------------------------------
def test_approve_like_creates_pending_queue_row(conn, config) -> None:
    media_pk = _candidate(conn)
    outcome = _service(conn, config).approve(media_pk, [ActionType.LIKE])

    row = conn.execute("SELECT * FROM action_queue WHERE media_pk = ?", (media_pk,)).fetchone()
    assert outcome.created == ["LIKE"]
    assert row["action_type"] == "LIKE"
    assert row["status"] == ActionStatus.PENDING.value   # 실행은 파이프라인이 한다
    assert row["approved_at"]                            # 승인 사실은 별도 기록
    assert conn.execute(
        "SELECT status FROM candidate_media WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0] == MediaStatus.QUEUED.value


def test_approve_comment_requires_selected_comment(conn, config) -> None:
    media_pk = _candidate(conn)
    service = _service(conn, config)

    outcome = service.approve(media_pk, [ActionType.COMMENT])
    assert outcome.created == [] and "선택된 댓글이 없어" in outcome.message

    service.select_comment(media_pk, text="퇴근 후 저녁이 제일 기다려지죠")
    outcome = service.approve(media_pk, [ActionType.COMMENT])
    row = conn.execute(
        "SELECT comment_text FROM action_queue WHERE media_pk = ? AND action_type = 'COMMENT'",
        (media_pk,),
    ).fetchone()
    assert outcome.created == ["COMMENT"]
    assert row["comment_text"] == "퇴근 후 저녁이 제일 기다려지죠"


def test_approve_like_and_comment_together(conn, config) -> None:
    media_pk = _candidate(conn)
    service = _service(conn, config)
    service.select_comment(media_pk, text="퇴근 후 저녁이 제일 기다려지죠")

    outcome = service.approve(media_pk, [ActionType.LIKE, ActionType.COMMENT])
    assert set(outcome.created) == {"LIKE", "COMMENT"}
    assert conn.execute(
        "SELECT COUNT(*) FROM action_queue WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0] == 2


def test_repeated_approval_is_idempotent(conn, config) -> None:
    """버튼을 여러 번 눌러도 Queue가 중복 생성되지 않는다."""
    media_pk = _candidate(conn)
    service = _service(conn, config)

    first = service.approve(media_pk, [ActionType.LIKE])
    second = service.approve(media_pk, [ActionType.LIKE])
    third = service.approve(media_pk, [ActionType.LIKE])

    assert first.created == ["LIKE"]
    assert second.already == ["LIKE"] and third.already == ["LIKE"]
    assert conn.execute(
        "SELECT COUNT(*) FROM action_queue WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0] == 1


def test_approval_warns_when_daily_limit_exceeded(conn, config) -> None:
    limited = Config(
        raw={**config.raw, "actions": {**config.raw["actions"],
             "daily_limits": {**config.raw["actions"]["daily_limits"], "likes": 0}}},
        path=config.path, base_dir=config.base_dir,
    )
    media_pk = _candidate(conn)
    outcome = DashboardService(conn, limited).approve(media_pk, [ActionType.LIKE])

    assert any("한도 초과" in w for w in outcome.warnings)
    assert outcome.created == ["LIKE"]   # 정책은 기존 controller/rate limiter가 실행 시 적용


# --- 12, 20. Skip / Undo ----------------------------------------------------
def test_skip_keeps_analysis_and_drafts(conn, config) -> None:
    media_pk = _candidate(conn)
    service = _service(conn, config)
    service.approve(media_pk, [ActionType.LIKE])
    service.skip(media_pk)

    assert conn.execute(
        "SELECT status FROM candidate_media WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0] == MediaStatus.SKIPPED.value
    assert conn.execute(
        "SELECT status FROM action_queue WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0] == ActionStatus.CANCELLED.value
    assert len(_drafts(conn, media_pk)) == 2                      # Draft 보존
    assert conn.execute("SELECT COUNT(*) FROM media_analysis").fetchone()[0] == 1


def test_reopen_returns_to_review_but_not_after_execution(conn, config) -> None:
    media_pk = _candidate(conn)
    service = _service(conn, config)
    service.skip(media_pk)

    ok, _ = service.reopen(media_pk)
    assert ok and conn.execute(
        "SELECT status FROM candidate_media WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0] == MediaStatus.SCORED.value

    record_interaction(
        conn, media_pk=media_pk, creator_id=1, action_id=None, action_type=ActionType.LIKE,
        executor="manual", dry_run=True, success=True,
    )
    conn.commit()
    ok, message = service.reopen(media_pk)
    assert ok is False and "되돌릴 수 없습니다" in message


# --- 13~17. Feedback --------------------------------------------------------
@pytest.mark.parametrize(
    "feedback",
    [FeedbackType.GOOD_TARGET, FeedbackType.NOT_MY_STYLE, FeedbackType.RESPONDED, FeedbackType.FOLLOWED],
)
def test_feedback_recorded(conn, config, feedback: FeedbackType) -> None:
    media_pk = _candidate(conn)
    ok, _ = _service(conn, config).add_feedback(media_pk, feedback)

    assert ok
    assert conn.execute(
        "SELECT feedback_type FROM feedback WHERE media_pk = ?", (media_pk,)
    ).fetchone()[0] == feedback.value


def test_duplicate_feedback_blocked(conn, config) -> None:
    media_pk = _candidate(conn)
    service = _service(conn, config)

    assert service.add_feedback(media_pk, FeedbackType.GOOD_TARGET)[0] is True
    ok, message = service.add_feedback(media_pk, FeedbackType.GOOD_TARGET)
    assert ok is False and "이미 기록" in message
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1


# --- 18~21. 렌더링 -----------------------------------------------------------
def test_review_page_renders_cards_and_summary(conn, config) -> None:
    _candidate(conn)
    html = render(conn, config, Page(tab="review", filters={}), token="t")

    assert "DailyReels Targeting Agent" in html
    assert "@creator_a" in html and "Claude" in html
    assert "검토 대기" in html and "LIKE 사용" in html      # Daily Limit 표시
    assert "claude -p" not in html


def test_needs_enrichment_shown_as_information_gap(conn, config) -> None:
    _candidate(conn, "URLONLY", "unresolved:URLONLY", score=0.0,
               status=MediaStatus.NEEDS_ENRICHMENT, analysis=None, drafts=())
    html = render(conn, config, Page(tab="review", filters={"status": "NEEDS_ENRICHMENT"}), token="t")

    assert "NEEDS_ENRICHMENT" in html
    assert "정보가 없습니다" in html
    assert "분석 대기" in html          # 오류가 아니라 상태로 표시


def test_detail_page_shows_analysis_method_and_interactions(conn, config) -> None:
    media_pk = _candidate(conn)
    record_interaction(
        conn, media_pk=media_pk, creator_id=1, action_id=None, action_type=ActionType.LIKE,
        executor="manual", dry_run=True, success=True,
    )
    conn.commit()

    html = render(conn, config, Page(tab="review", media_pk=media_pk), token="tok")
    assert "Instagram에서 열기" in html and "https://www.instagram.com/reel/M1/" in html
    assert "관련성" in html and "0.92" in html
    assert "직장인 퇴근 맥락이 겹친다" in html
    assert "content_similarity" in html                # 점수 구성요소 표시
    assert "최근 Interaction" in html
    assert 'value="tok"' in html                        # POST 토큰 포함


def test_queue_and_stats_tabs_render(conn, config) -> None:
    media_pk = _candidate(conn)
    _service(conn, config).approve(media_pk, [ActionType.LIKE])
    _service(conn, config).add_feedback(media_pk, FeedbackType.GOOD_TARGET)

    queue_html = render(conn, config, Page(tab="queue"), token="t")
    assert "Action Queue" in queue_html and "PENDING" in queue_html
    assert "<form" not in queue_html          # 실행을 트리거하는 폼이 없다

    stats_html = render(conn, config, Page(tab="stats"), token="t")
    assert "GOOD_TARGET" in stats_html and "수동 입력" in stats_html
