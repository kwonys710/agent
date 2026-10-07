"""Phase 17.1 — 운영 UX / 통계 Hotfix 테스트.

실제 Claude CLI·Instagram 호출 없음.
"""
from __future__ import annotations

import pytest

from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    count_by_final_analyzer,
    enqueue_action,
    final_analyzer_of,
    insert_candidate,
    save_analysis,
    today_str,
    update_candidate_status,
    utc_now,
)
from targeting_agent.core.models import (
    ActionType,
    ContentAnalysis,
    MediaStatus,
    RawCandidate,
)
from targeting_agent.dashboard.app import Page, render
from targeting_agent.dashboard.queries import (
    candidate_detail,
    daily_summary,
    default_action_selection,
)
from targeting_agent.dashboard.service import DashboardService
from targeting_agent.learning.metrics import collect_quality_metrics


def _detail(score, status=MediaStatus.SCORED.value, actions=()):
    return {"target_score": score, "status": status, "actions": list(actions)}


def _action_row(action_type: str, status: str = "PENDING") -> dict:
    return {"action_type": action_type, "status": status}


def _thresholds(config: Config, like: float, comment: float) -> Config:
    actions = {
        **config.raw["actions"],
        "require_score_for_like": like,
        "require_score_for_comment": comment,
    }
    return Config(raw={**config.raw, "actions": actions}, path=config.path, base_dir=config.base_dir)


def _candidate(conn, media_id="M1", score=88.0, username=None, analyzers=("heuristic",), date=None):
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
    for analyzer in analyzers:
        save_analysis(
            conn,
            media_pk,
            ContentAnalysis(topics=["직장생활"], analyzer=analyzer, analyzer_version="1"),
            target_score=score,
        )
        if date:
            conn.execute(
                "UPDATE media_analysis SET analyzed_at = ? WHERE media_pk = ? AND analyzer = ?",
                (date, media_pk, analyzer),
            )
    update_candidate_status(conn, media_pk, MediaStatus.SCORED, "test", score)
    conn.commit()
    return int(media_pk)


# --- Action 기본 선택 (1~6) ---------------------------------------------------
@pytest.mark.parametrize(
    "score,expected_like,expected_comment",
    [
        (66.0, False, False),   # 실제 운영에서 발견된 사례
        (74.9, False, False),
        (75.0, True, False),
        (81.0, True, False),
        (82.0, True, True),
        (100.0, True, True),
    ],
)
def test_defaults_follow_thresholds(config, score, expected_like, expected_comment) -> None:
    defaults = default_action_selection(_detail(score), _thresholds(config, 75, 82))

    assert defaults.like is expected_like
    assert defaults.comment is expected_comment
    assert defaults.source == "threshold"


def test_score_66_case_shows_notice(config) -> None:
    """운영에서 발견된 회귀 사례: Score 66이면 둘 다 해제되고 기준을 안내한다."""
    defaults = default_action_selection(_detail(66.0), _thresholds(config, 75, 82))

    assert (defaults.like, defaults.comment) == (False, False)
    assert "현재 Score 66" in defaults.notice
    assert "LIKE 추천 기준 75 미달" in defaults.notice
    assert "COMMENT 추천 기준 82 미달" in defaults.notice
    assert "직접 선택" in defaults.notice


def test_threshold_change_moves_defaults(config) -> None:
    defaults = default_action_selection(_detail(66.0), _thresholds(config, 60, 65))
    assert (defaults.like, defaults.comment) == (True, True)


def test_missing_score_defaults_off(config) -> None:
    defaults = default_action_selection(_detail(None), _thresholds(config, 75, 82))
    assert (defaults.like, defaults.comment) == (False, False)


# --- 기존 선택 보존 / Skip (8~9) ------------------------------------------------
def test_existing_queue_selection_is_preserved(config) -> None:
    """임계값 미달이어도 이미 만들어진 선택을 기본값이 덮어쓰지 않는다."""
    detail = _detail(66.0, actions=[_action_row("LIKE"), _action_row("COMMENT")])
    defaults = default_action_selection(detail, _thresholds(config, 75, 82))

    assert (defaults.like, defaults.comment) == (True, True)
    assert defaults.source == "queue"


def test_cancelled_queue_rows_do_not_count(config) -> None:
    detail = _detail(95.0, actions=[_action_row("LIKE", "CANCELLED")])
    defaults = default_action_selection(detail, _thresholds(config, 75, 82))

    assert defaults.source == "threshold" and defaults.like is True


def test_skipped_candidate_defaults_off(config) -> None:
    defaults = default_action_selection(
        _detail(95.0, status=MediaStatus.SKIPPED.value), _thresholds(config, 75, 82)
    )
    assert (defaults.like, defaults.comment) == (False, False)
    assert defaults.source == "skipped"


def test_detail_html_reflects_defaults(conn, config) -> None:
    media_pk = _candidate(conn, score=66.0)
    html = render(conn, _thresholds(config, 75, 82), Page(tab="review", media_pk=media_pk), token="t")

    assert 'name="like"> LIKE' in html            # 체크되지 않은 상태
    assert 'name="comment"> COMMENT' in html
    assert "LIKE 추천 기준 75 미달" in html


def test_detail_html_checks_when_score_high(conn, config) -> None:
    media_pk = _candidate(conn, score=90.0)
    html = render(conn, _thresholds(config, 75, 82), Page(tab="review", media_pk=media_pk), token="t")

    assert 'name="like" checked' in html and 'name="comment" checked' in html


# --- 수동 override / 승인 유지 (7, 10) -------------------------------------------
def test_manual_override_below_threshold_is_allowed(conn, config) -> None:
    """추천 기준 미달이어도 운영자가 직접 승인할 수 있다(차단하지 않는다)."""
    media_pk = _candidate(conn, score=66.0)
    service = DashboardService(conn, _thresholds(config, 75, 82))

    outcome = service.approve(media_pk, [ActionType.LIKE])
    assert outcome.created == ["LIKE"]

    row = conn.execute("SELECT status, approved_at FROM action_queue").fetchone()
    assert row["status"] == "PENDING" and row["approved_at"]   # Approval Gate 유지


def test_repeated_approval_still_idempotent(conn, config) -> None:
    media_pk = _candidate(conn, score=90.0)
    service = DashboardService(conn, config)
    service.approve(media_pk, [ActionType.LIKE])
    service.approve(media_pk, [ActionType.LIKE])

    assert conn.execute("SELECT COUNT(*) FROM action_queue").fetchone()[0] == 1


def test_defaults_after_approval_come_from_queue(conn, config) -> None:
    media_pk = _candidate(conn, score=66.0)
    DashboardService(conn, _thresholds(config, 75, 82)).approve(media_pk, [ActionType.LIKE])

    detail = candidate_detail(conn, media_pk)
    defaults = default_action_selection(detail, _thresholds(config, 75, 82))
    assert (defaults.like, defaults.comment) == (True, False)
    assert defaults.source == "queue"


# --- 통계 (21~24) ----------------------------------------------------------------
def test_final_analyzer_counts_each_candidate_once(conn, config) -> None:
    """Claude 분석 후보는 heuristic 행도 갖는다. 후보가 양쪽에 중복 집계되면 안 된다."""
    _candidate(conn, "A", analyzers=("heuristic", "claude_code"))   # Claude 성공
    _candidate(conn, "B", analyzers=("heuristic", "claude_code"))   # Cache로 Claude 결과 재사용
    _candidate(conn, "C", analyzers=("heuristic",))                 # Claude 실패 → heuristic
    _candidate(conn, "D", analyzers=("heuristic",))                 # Claude 불가 → heuristic

    counts = count_by_final_analyzer(conn)
    assert counts == {"claude_code": 2, "heuristic": 2}
    assert sum(counts.values()) == 4                                 # 후보 수와 일치


def test_dashboard_summary_uses_final_analyzer(conn, config) -> None:
    _candidate(conn, "A", analyzers=("heuristic", "claude_code"))
    _candidate(conn, "B", analyzers=("heuristic",))

    summary = daily_summary(conn)
    assert summary["claude_analyzed"] == 1
    assert summary["heuristic_analyzed"] == 1


def test_claude_calls_and_analyzed_are_separate(conn, config) -> None:
    """Claude 호출(시도)과 Claude 분석(최종 방식)은 다른 값이며 서로 달라도 정상이다."""
    from targeting_agent.core.database import bump_stat

    bump_stat(conn, today_str(9), "claude_requests", 3)   # A 성공, C 실패, D 불가 시도
    _candidate(conn, "A", analyzers=("heuristic", "claude_code"))
    _candidate(conn, "B", analyzers=("heuristic", "claude_code"))   # Cache Hit(호출 없음)
    _candidate(conn, "C", analyzers=("heuristic",))
    _candidate(conn, "D", analyzers=("heuristic",))
    conn.commit()

    summary = daily_summary(conn)
    assert summary["claude_calls"] == 3         # 호출 시도
    assert summary["claude_analyzed"] == 2      # 최종 방식이 Claude인 후보
    assert summary["heuristic_analyzed"] == 2   # fallback 포함


def test_analyzed_card_matches_analyzer_cards(conn, config) -> None:
    """'분석 완료'와 Claude/Heuristic 카드가 같은 기준이어야 한다."""
    _candidate(conn, "A", analyzers=("heuristic", "claude_code"))
    _candidate(conn, "B", analyzers=("heuristic",))
    _candidate(conn, "OLD", analyzers=("heuristic",), date="2026-01-01T00:00:00+00:00")

    summary = daily_summary(conn)
    assert summary["analyzed"] == summary["claude_analyzed"] + summary["heuristic_analyzed"]
    assert summary["analyzed"] == 2      # 어제 분석분은 제외


def test_summary_date_boundary(conn, config) -> None:
    _candidate(conn, "TODAY", analyzers=("heuristic", "claude_code"))
    _candidate(conn, "YESTERDAY", analyzers=("heuristic",), date="2026-01-01T00:00:00+00:00")

    summary = daily_summary(conn)
    assert summary["claude_analyzed"] == 1
    assert summary["heuristic_analyzed"] == 0     # 어제 후보는 오늘 카드에 들어오지 않는다
    assert count_by_final_analyzer(conn)["heuristic"] == 1   # 누적으로는 보인다


def test_final_analyzer_of_single_candidate(conn, config) -> None:
    claude_pk = _candidate(conn, "A", analyzers=("heuristic", "claude_code"))
    heuristic_pk = _candidate(conn, "B", analyzers=("heuristic",))

    assert final_analyzer_of(conn, claude_pk) == "claude_code"
    assert final_analyzer_of(conn, heuristic_pk) == "heuristic"
    assert final_analyzer_of(conn, 9999) is None


def test_quality_metrics_split_by_final_analyzer(conn, config) -> None:
    claude_pk = _candidate(conn, "A", analyzers=("heuristic", "claude_code"))
    _candidate(conn, "B", analyzers=("heuristic",))
    action_id = enqueue_action(
        conn, run_id="r", media_pk=claude_pk, creator_id=1, action_type=ActionType.LIKE,
        target_score=90.0, priority=90, executor="manual", dry_run=True,
    )
    conn.execute(
        "UPDATE action_queue SET approved_at = ? WHERE action_id = ?", (utc_now(), action_id)
    )
    conn.commit()

    metrics = collect_quality_metrics(conn)
    assert metrics.claude_approval_rate == 1.0      # Claude 후보 1건 중 1건 승인
    assert metrics.heuristic_approval_rate == 0.0   # heuristic 후보는 분모에 1건만
