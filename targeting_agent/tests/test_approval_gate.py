"""Phase 14.1 — Approval Gate 테스트.

실행 가능 조건: status == 'PENDING' AND approved_at IS NOT NULL
승인은 실행 조건 중 하나일 뿐이며, 기존 Safety/Rate Limit 조건을 대체하지 않는다.
실제 Instagram / Claude 호출은 없다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.actions.controller import ActionController
from targeting_agent.actions.executors.manual import ManualExecutor
from targeting_agent.actions.queue import ActionQueueBuilder
from targeting_agent.actions.rate_limiter import RateLimiter
from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    count_unapproved_actions,
    enqueue_action,
    fetch_executable_actions,
    insert_candidate,
    record_interaction,
    update_action_status,
    utc_now,
)
from targeting_agent.core.models import ActionStatus, ActionType, RawCandidate
from targeting_agent.dashboard.service import DashboardService


def _candidate(conn, media_id="M1", username="creator_a") -> int:
    media_pk = insert_candidate(
        conn,
        RawCandidate(
            media_id=media_id,
            permalink=f"https://www.instagram.com/reel/{media_id}/",
            username=username,
            caption="퇴근 후 저녁 #퇴근",
            hashtags=["퇴근"],
            followers=5000,
        ),
    )
    conn.execute(
        "UPDATE candidate_media SET target_score = 90 WHERE media_pk = ?", (media_pk,)
    )
    conn.commit()
    return int(media_pk)


def _enqueue(conn, media_pk, action_type=ActionType.LIKE, comment=None, creator_id=1) -> int:
    action_id = enqueue_action(
        conn, run_id="r1", media_pk=media_pk, creator_id=creator_id, action_type=action_type,
        target_score=90.0, priority=90, executor="manual", dry_run=True, comment_text=comment,
    )
    conn.commit()
    return int(action_id)


def _approve(conn, action_id: int, at: str | None = None) -> None:
    conn.execute(
        "UPDATE action_queue SET approved_at = ? WHERE action_id = ?", (at or utc_now(), action_id)
    )
    conn.commit()


# --- 1~2. 핵심 게이트 -------------------------------------------------------
def test_unapproved_pending_is_never_executable(conn) -> None:
    media_pk = _candidate(conn)
    _enqueue(conn, media_pk)

    assert fetch_executable_actions(conn) == []
    assert count_unapproved_actions(conn) == 1


def test_approved_pending_is_executable(conn) -> None:
    media_pk = _candidate(conn)
    action_id = _enqueue(conn, media_pk)
    _approve(conn, action_id)

    rows = fetch_executable_actions(conn)
    assert [int(r["action_id"]) for r in rows] == [action_id]
    assert count_unapproved_actions(conn) == 0


# --- 3. Phase 9 legacy APPROVED --------------------------------------------
def test_legacy_approved_status_stays_out_of_execution(conn) -> None:
    """Phase 9의 APPROVED(수동 처리 후 확인 대기)는 재실행 대상이 아니다."""
    media_pk = _candidate(conn)
    action_id = _enqueue(conn, media_pk)
    _approve(conn, action_id)
    update_action_status(conn, action_id, ActionStatus.APPROVED)
    conn.commit()

    assert fetch_executable_actions(conn) == []
    # --confirm 흐름은 그대로 동작한다
    from targeting_agent.actions.confirm import list_awaiting

    assert [int(r["action_id"]) for r in list_awaiting(conn)] == [action_id]


# --- 4~6. 생성 / 승인 -------------------------------------------------------
def test_pipeline_generated_action_is_unapproved(config, conn) -> None:
    media_pk = _candidate(conn)
    row = dict(
        conn.execute("SELECT * FROM candidate_media WHERE media_pk = ?", (media_pk,)).fetchone()
    )
    ActionQueueBuilder(config, run_id="auto").build(conn, row, 95.0, comment_text="퇴근 분위기 좋네요")
    conn.commit()

    rows = conn.execute("SELECT status, approved_at FROM action_queue").fetchall()
    assert rows and all(r["status"] == "PENDING" and r["approved_at"] is None for r in rows)
    assert fetch_executable_actions(conn) == []


def test_dashboard_approval_sets_approved_at(config, conn) -> None:
    media_pk = _candidate(conn)
    _enqueue(conn, media_pk)

    DashboardService(conn, config).approve(media_pk, [ActionType.LIKE])
    row = conn.execute("SELECT status, approved_at FROM action_queue").fetchone()

    assert row["status"] == "PENDING" and row["approved_at"]
    assert len(fetch_executable_actions(conn)) == 1


def test_repeated_approval_keeps_single_row_and_first_timestamp(config, conn) -> None:
    media_pk = _candidate(conn)
    _enqueue(conn, media_pk)
    service = DashboardService(conn, config)

    service.approve(media_pk, [ActionType.LIKE])
    first = conn.execute("SELECT approved_at FROM action_queue").fetchone()["approved_at"]
    service.approve(media_pk, [ActionType.LIKE])
    service.approve(media_pk, [ActionType.LIKE])

    rows = conn.execute("SELECT approved_at FROM action_queue").fetchall()
    assert len(rows) == 1                       # Queue 중복 없음
    assert rows[0]["approved_at"] == first      # 최초 승인 시각 유지


# --- 7. Manual Executor 노출 -------------------------------------------------
def test_unapproved_action_not_shown_to_manual_executor(config, conn, tmp_path) -> None:
    media_pk = _candidate(conn)
    _enqueue(conn, media_pk)   # 승인하지 않음

    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=True)
    summary = ActionController(config, conn, executor).run()

    assert summary.success == 0 and summary.skipped == 0
    assert summary.awaiting_approval == 1
    assert not executor.export_path.exists()    # 수동 처리 목록에도 실리지 않는다
    assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0


def test_approved_action_reaches_manual_executor(config, conn, tmp_path) -> None:
    media_pk = _candidate(conn)
    action_id = _enqueue(conn, media_pk)
    _approve(conn, action_id)

    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=True)
    summary = ActionController(config, conn, executor).run()

    assert summary.success == 1
    assert executor.export_path.exists()


# --- 8~9. 승인해도 Safety 조건은 그대로 --------------------------------------
def test_approved_action_blocked_by_daily_limit(config, conn, tmp_path) -> None:
    limited = Config(
        raw={**config.raw, "actions": {**config.raw["actions"],
             "daily_limits": {**config.raw["actions"]["daily_limits"], "likes": 0}}},
        path=config.path, base_dir=config.base_dir,
    )
    media_pk = _candidate(conn)
    _approve(conn, _enqueue(conn, media_pk))

    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=True)
    summary = ActionController(limited, conn, executor).run()

    assert summary.success == 0 and summary.skipped == 1
    assert any("daily_limit" in reason for reason in summary.skip_reasons)
    assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0


def test_approved_action_blocked_by_creator_cooldown(config, conn, tmp_path) -> None:
    first_pk = _candidate(conn, "M1", "creator_a")
    second_pk = _candidate(conn, "M2", "creator_a")   # 같은 Creator의 다른 게시물
    record_interaction(
        conn, media_pk=first_pk, creator_id=1, action_id=None, action_type=ActionType.LIKE,
        executor="manual", dry_run=True, success=True,
    )
    _approve(conn, _enqueue(conn, second_pk))

    limiter = RateLimiter(config, conn, dry_run=True)
    decision = limiter.check(ActionType.LIKE, 1, second_pk)
    assert decision.allowed is False and "creator" in decision.reason

    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=True)
    summary = ActionController(config, conn, executor).run()
    assert summary.success == 0 and summary.skipped == 1


def test_approved_action_blocked_by_duplicate_interaction(config, conn, tmp_path) -> None:
    media_pk = _candidate(conn)
    record_interaction(
        conn, media_pk=media_pk, creator_id=1, action_id=None, action_type=ActionType.LIKE,
        executor="manual", dry_run=True, success=True,
    )
    _approve(conn, _enqueue(conn, media_pk))

    summary = ActionController(
        config, conn, ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=True)
    ).run()

    assert summary.success == 0
    assert any("already_interacted" in reason for reason in summary.skip_reasons)
