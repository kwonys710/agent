"""Phase 9 — 수동 처리 확인(Confirm) 흐름 테스트.

실제 모드에서 목록만 만든 것을 '처리 완료'로 기록하지 않는다는 점을 검증한다.
"""
from __future__ import annotations

import pytest

from targeting_agent.actions.confirm import (
    confirm_actions,
    format_awaiting,
    list_awaiting,
    parse_ids,
    skip_actions,
)
from targeting_agent.actions.controller import ActionController
from targeting_agent.actions.executors.manual import ManualExecutor
from targeting_agent.actions.rate_limiter import RateLimiter
from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    enqueue_action,
    utc_now,
    fetch_executable_actions,
    insert_candidate,
    update_action_status,
)
from targeting_agent.core.models import ActionStatus, ActionType, MediaStatus, QueuedAction


def _real_config(config: Config) -> Config:
    """dry_run=False 인 설정 사본."""
    raw = dict(config.raw)
    raw["actions"] = {
        **raw["actions"],
        "execution": {**raw["actions"]["execution"], "dry_run": False},
    }
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def _queue_one(
    conn, candidate, action_type=ActionType.LIKE, comment=None, run_id="r1", approved=True
) -> int:
    """Action을 큐에 넣는다. approved=True면 운영자 승인까지 마친 상태로 만든다."""
    media_pk = insert_candidate(conn, candidate)
    action_id = enqueue_action(
        conn, run_id=run_id, media_pk=media_pk, creator_id=1, action_type=action_type,
        target_score=90.0, priority=90, executor="manual", dry_run=False,
        comment_text=comment,
    )
    if approved:
        conn.execute(
            "UPDATE action_queue SET approved_at = ? WHERE action_id = ?",
            (utc_now(), int(action_id)),
        )
    conn.commit()
    return int(action_id)


def _action(action_id: int = 1, action_type=ActionType.LIKE, comment=None) -> QueuedAction:
    return QueuedAction(
        action_id=action_id, media_pk=1, media_id="M1",
        permalink="https://www.instagram.com/reel/M1/", username="tester", creator_id=1,
        action_type=action_type, target_score=90.0, priority=90, comment_text=comment,
    )


# --- ManualExecutor ------------------------------------------------------
def test_real_mode_returns_awaiting_not_success(tmp_path) -> None:
    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=False)
    result = executor.execute_like(_action())
    assert result.status is ActionStatus.APPROVED
    assert result.success is False
    assert "awaiting_manual_confirm" in result.detail


def test_dry_run_still_reports_success(tmp_path) -> None:
    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=True)
    assert executor.execute_like(_action()).status is ActionStatus.SUCCESS


def test_checklist_contains_permalink_and_comment(tmp_path) -> None:
    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=False)
    executor.execute_comment(_action(7, ActionType.COMMENT, "퇴근 분위기 좋네요"))
    executor.finalize()

    text = executor.checklist_path.read_text(encoding="utf-8")
    assert "https://www.instagram.com/reel/M1/" in text
    assert "퇴근 분위기 좋네요" in text
    assert "Action ID `7`" in text
    assert "--confirm" in text


# --- Controller ----------------------------------------------------------
def test_controller_does_not_record_interaction_for_awaiting(config, conn, sample_candidate, tmp_path) -> None:
    real = _real_config(config)
    action_id = _queue_one(conn, sample_candidate)
    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=False)

    summary = ActionController(real, conn, executor).run()

    assert summary.awaiting == 1
    assert summary.success == 0
    assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0
    status = conn.execute(
        "SELECT status FROM action_queue WHERE action_id = ?", (action_id,)
    ).fetchone()[0]
    assert status == ActionStatus.APPROVED.value


def test_awaiting_actions_not_re_executed(config, conn, sample_candidate) -> None:
    action_id = _queue_one(conn, sample_candidate)
    update_action_status(conn, action_id, ActionStatus.APPROVED)
    conn.commit()
    assert fetch_executable_actions(conn) == []


def test_awaiting_counts_toward_daily_limit(config, conn, sample_candidate) -> None:
    """확인 대기 건이 한도에 잡혀야 한도를 넘는 목록이 만들어지지 않는다."""
    raw = dict(config.raw)
    raw["actions"] = {**raw["actions"], "daily_limits": {**raw["actions"]["daily_limits"], "likes": 1}}
    limited = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    action_id = _queue_one(conn, sample_candidate)
    update_action_status(conn, action_id, ActionStatus.APPROVED)
    conn.commit()

    limiter = RateLimiter(limited, conn, dry_run=False)
    assert limiter.used_today(ActionType.LIKE) == 1
    decision = limiter.check(ActionType.LIKE, 1, 999)
    assert decision.allowed is False and "daily_limit" in decision.reason


# --- Confirm / Skip ------------------------------------------------------
def test_parse_ids() -> None:
    assert parse_ids("all") == (True, [])
    assert parse_ids("12, 13") == (False, [12, 13])
    with pytest.raises(ValueError):
        parse_ids("abc")


def test_confirm_records_real_interaction(config, conn, sample_candidate) -> None:
    action_id = _queue_one(conn, sample_candidate, ActionType.COMMENT, "퇴근 분위기 좋네요")
    update_action_status(conn, action_id, ActionStatus.APPROVED)
    conn.commit()

    result = confirm_actions(conn, config, action_ids=[action_id])
    assert result.confirmed == 1

    interaction = conn.execute("SELECT * FROM interactions").fetchone()
    assert interaction["dry_run"] == 0 and interaction["success"] == 1
    assert interaction["comment_text"] == "퇴근 분위기 좋네요"
    assert conn.execute(
        "SELECT status FROM action_queue WHERE action_id = ?", (action_id,)
    ).fetchone()[0] == ActionStatus.SUCCESS.value
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM candidate_media WHERE media_pk = ?", (interaction["media_pk"],)
    ).fetchone()[0] == MediaStatus.INTERACTED.value


def test_confirm_reports_unknown_ids(config, conn, sample_candidate) -> None:
    action_id = _queue_one(conn, sample_candidate)
    update_action_status(conn, action_id, ActionStatus.APPROVED)
    conn.commit()
    result = confirm_actions(conn, config, action_ids=[action_id, 4242])
    assert result.confirmed == 1 and result.not_found == [4242]


def test_confirm_all_warns_over_daily_limit(config, conn, sample_candidate) -> None:
    from targeting_agent.core.models import RawCandidate

    raw = dict(config.raw)
    raw["actions"] = {**raw["actions"], "daily_limits": {**raw["actions"]["daily_limits"], "likes": 1}}
    limited = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    for index in range(2):
        candidate = RawCandidate(
            media_id=f"L{index}", permalink="p", username=f"user_{index}", followers=3000
        )
        media_pk = insert_candidate(conn, candidate)
        action_id = enqueue_action(
            conn, run_id="r1", media_pk=media_pk, creator_id=index + 1,
            action_type=ActionType.LIKE, target_score=90.0, priority=90,
            executor="manual", dry_run=False,
        )
        update_action_status(conn, int(action_id), ActionStatus.APPROVED)
    conn.commit()

    result = confirm_actions(conn, limited, select_all=True)
    # 이미 처리한 일은 기록하되, 한도 초과는 경고로 남긴다
    assert result.confirmed == 2
    assert result.over_limit and "일일 한도" in result.over_limit[0]


def test_skip_cancels_without_interaction(config, conn, sample_candidate) -> None:
    action_id = _queue_one(conn, sample_candidate)
    update_action_status(conn, action_id, ActionStatus.APPROVED)
    conn.commit()

    result = skip_actions(conn, action_ids=[action_id])
    assert result.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM action_queue WHERE action_id = ?", (action_id,)
    ).fetchone()[0] == ActionStatus.CANCELLED.value


def test_list_and_format_awaiting(config, conn, sample_candidate) -> None:
    assert "없습니다" in format_awaiting(list_awaiting(conn))

    action_id = _queue_one(conn, sample_candidate, ActionType.COMMENT, "퇴근 분위기 좋네요")
    update_action_status(conn, action_id, ActionStatus.APPROVED)
    conn.commit()

    text = format_awaiting(list_awaiting(conn))
    assert "office_daily_kim" in text and "퇴근 분위기 좋네요" in text
