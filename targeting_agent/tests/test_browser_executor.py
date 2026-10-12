"""Browser Action Executor 테스트 (Phase 18B).

실제 Instagram에 쓰기 동작을 하지 않는다. LIVE 경로는 FakeActionPage와
(별도 파일의) 로컬 DOM fixture로만 검증한다.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import pytest

from targeting_agent.actions.controller import ActionController
from targeting_agent.actions.executors import action_selectors as ASEL
from targeting_agent.actions.executors.browser import BrowserExecutor, ExecutorMode
from targeting_agent.actions.executors.browser_page import STOP_ACTION_BLOCKED
from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    approve_action,
    enqueue_action,
    record_interaction,
    insert_candidate,
    upsert_creator,
)
from targeting_agent.core.exceptions import PlatformWarningError
from targeting_agent.core.models import (
    ActionStatus,
    ActionType,
    QueuedAction,
    RawCandidate,
)
from targeting_agent.discovery.browser_models import SessionState


class FakeActionPage:
    """쓰기 동작 대역. 무엇을 호출했는지 기록만 한다."""

    def __init__(
        self,
        *,
        stop: Optional[str] = None,
        stop_after_action: Optional[str] = None,
        already_liked: bool = False,
        like_ok: bool = True,
        comment_ok: bool = True,
    ) -> None:
        self.stop = stop
        self.stop_after_action = stop_after_action
        self._already_liked = already_liked
        self.like_ok = like_ok
        self.comment_ok = comment_ok
        self.opened: list[str] = []
        self.likes = 0
        self.comments: list[str] = []
        self.closed = False
        self._acted = False

    def ensure_logged_in(self) -> SessionState:
        if self.stop in (
            SessionState.LOGIN_REQUIRED.value,
            SessionState.CHALLENGE.value,
            SessionState.PLATFORM_WARNING.value,
        ):
            return SessionState(self.stop)
        return SessionState.LOGGED_IN

    def open_media(self, url: str) -> None:
        self.opened.append(url)

    def stop_reason(self) -> Optional[str]:
        if self._acted and self.stop_after_action:
            return self.stop_after_action
        return self.stop

    def already_liked(self) -> bool:
        return self._already_liked

    def like(self) -> bool:
        self.likes += 1
        self._acted = True
        self._already_liked = self.like_ok
        return self.like_ok

    def comment(self, text: str) -> bool:
        self.comments.append(text)
        self._acted = True
        return self.comment_ok

    def close(self) -> None:
        self.closed = True


def live_config(config: Config, **overrides) -> Config:
    """browser executor를 LIVE로 돌린 설정(실제 쓰기는 FakeActionPage가 받는다)."""
    raw = {
        **config.raw,
        "executor": {**config.raw.get("executor", {}), "mode": "browser"},
        "browser_executor": {"mode": "LIVE", **overrides},
        "actions": {
            **config.raw["actions"],
            "execution": {**config.raw["actions"]["execution"], "dry_run": False},
        },
    }
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def seed_candidate(
    conn: sqlite3.Connection, *, code: str = "AAA111", username: str = "creator_a"
) -> tuple[int, int]:
    candidate = RawCandidate(
        media_id=code,
        permalink=f"https://www.instagram.com/reel/{code}/",
        canonical_url=f"https://www.instagram.com/reel/{code}/",
        username=username,
        caption="퇴근 후 저녁 #직장인 #퇴근후",
        hashtags=["직장인", "퇴근후"],
        followers=5000,
    )
    creator_id = upsert_creator(conn, candidate)
    media_pk = insert_candidate(conn, candidate)
    conn.commit()
    assert media_pk is not None
    return int(media_pk), creator_id


def action_of(
    media_pk: int,
    creator_id: int,
    action_type: ActionType = ActionType.LIKE,
    comment_text: Optional[str] = None,
) -> QueuedAction:
    return QueuedAction(
        action_id=1,
        media_pk=media_pk,
        media_id="AAA111",
        permalink="https://www.instagram.com/reel/AAA111/",
        username="creator_a",
        creator_id=creator_id,
        action_type=action_type,
        target_score=90.0,
        priority=90,
        status=ActionStatus.PENDING,
        comment_text=comment_text,
        executor="browser",
    )


# --- Mode -----------------------------------------------------------------
def test_mode_파싱은_안전한_기본값을_쓴다():
    assert ExecutorMode.parse("LIVE") is ExecutorMode.LIVE
    assert ExecutorMode.parse("live") is ExecutorMode.LIVE
    assert ExecutorMode.parse("DISABLED") is ExecutorMode.DISABLED
    assert ExecutorMode.parse(None) is ExecutorMode.DRY_RUN
    assert ExecutorMode.parse("무엇인가") is ExecutorMode.DRY_RUN  # 모르는 값 → DRY_RUN


def test_disabled면_아무것도_하지_않는다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=False, config=config, conn=conn, page=page, mode="DISABLED")

    result = executor.execute_like(action_of(media_pk, creator_id))

    assert result.status is ActionStatus.SKIPPED
    assert result.detail == "browser_executor_disabled"
    assert page.opened == [] and page.likes == 0


def test_dry_run은_브라우저를_열지_않고_계획만_만든다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=True, config=config, conn=conn, page=page, mode="DRY_RUN")

    like = executor.execute_like(action_of(media_pk, creator_id))
    comment = executor.execute_comment(
        action_of(media_pk, creator_id, ActionType.COMMENT, "퇴근 분위기 좋네요")
    )

    assert like.status is ActionStatus.SUCCESS
    assert "dry_run:LIKE https://www.instagram.com/reel/AAA111/" == like.detail
    assert comment.status is ActionStatus.SUCCESS
    assert "comment=퇴근 분위기 좋네요" in comment.detail
    # 실제 동작은 0건
    assert page.opened == [] and page.likes == 0 and page.comments == []


def test_live인데_config_dry_run이면_실제_동작하지_않는다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=True, config=config, conn=conn, page=page, mode="LIVE")

    result = executor.execute_like(action_of(media_pk, creator_id))

    assert executor.live is False
    assert result.detail.startswith("dry_run:LIKE")
    assert page.likes == 0


# --- LIVE 경로 ------------------------------------------------------------
def test_live_like가_수행되고_확인된다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    result = executor.execute_like(action_of(media_pk, creator_id))

    assert executor.live is True
    assert result.status is ActionStatus.SUCCESS
    assert page.opened == ["https://www.instagram.com/reel/AAA111/"]
    assert page.likes == 1


def test_이미_좋아요한_게시물은_다시_누르지_않는다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage(already_liked=True)
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    result = executor.execute_like(action_of(media_pk, creator_id))

    assert result.status is ActionStatus.SKIPPED
    assert result.detail == "already_liked"
    assert page.likes == 0


def test_like_확인_실패는_재시도하지_않고_실패로_남긴다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage(like_ok=False)
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    result = executor.execute_like(action_of(media_pk, creator_id))

    assert result.status is ActionStatus.FAILED
    assert result.error == "like_not_confirmed"
    assert page.likes == 1  # 한 번만 시도한다


def test_live_comment가_기존_댓글_텍스트를_그대로_쓴다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    text = "저도 퇴근하고 이렇게 보내고 싶네요"
    result = executor.execute_comment(action_of(media_pk, creator_id, ActionType.COMMENT, text))

    assert result.status is ActionStatus.SUCCESS
    assert page.comments == [text]  # Executor가 댓글을 새로 만들지 않는다


def test_댓글_텍스트가_없으면_실행하지_않는다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    result = executor.execute_comment(action_of(media_pk, creator_id, ActionType.COMMENT, None))

    assert result.status is ActionStatus.SKIPPED
    assert result.detail == "no_comment_text"
    assert page.comments == []


def test_이미_실제로_실행한_action은_건너뛴다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    record_interaction(
        conn,
        media_pk=media_pk,
        creator_id=creator_id,
        action_id=None,
        action_type=ActionType.LIKE,
        executor="browser",
        dry_run=False,
        success=True,
    )
    conn.commit()
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    result = executor.execute_like(action_of(media_pk, creator_id))

    assert result.status is ActionStatus.SKIPPED
    assert result.detail == "already_executed"
    assert page.likes == 0


def test_dry_run_기록은_중복으로_보지_않는다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    record_interaction(
        conn,
        media_pk=media_pk,
        creator_id=creator_id,
        action_id=None,
        action_type=ActionType.LIKE,
        executor="browser",
        dry_run=True,
        success=True,
    )
    conn.commit()
    page = FakeActionPage()
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    assert executor.execute_like(action_of(media_pk, creator_id)).status is ActionStatus.SUCCESS


# --- 중단 조건 ------------------------------------------------------------
@pytest.mark.parametrize(
    "stop",
    [
        SessionState.LOGIN_REQUIRED.value,
        SessionState.CHALLENGE.value,
        SessionState.PLATFORM_WARNING.value,
        STOP_ACTION_BLOCKED,
    ],
)
def test_중단_상태면_action_run_전체를_멈춘다(config: Config, conn: sqlite3.Connection, stop: str):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage(stop=stop)
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    with pytest.raises(PlatformWarningError) as exc:
        executor.execute_like(action_of(media_pk, creator_id))

    assert stop in str(exc.value)
    assert page.likes == 0


def test_동작_직후_차단이_감지되면_중단한다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    page = FakeActionPage(stop_after_action=STOP_ACTION_BLOCKED)
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    with pytest.raises(PlatformWarningError):
        executor.execute_like(action_of(media_pk, creator_id))

    assert page.likes == 1  # 눌렀지만 이후 차단을 감지해 Run을 멈춘다


def test_중단_상태면_세션_검증에서_실패한다(config: Config, conn: sqlite3.Connection):
    page = FakeActionPage(stop=SessionState.CHALLENGE.value)
    executor = BrowserExecutor(dry_run=False, config=live_config(config), conn=conn, page=page)

    assert executor.validate_session() is False


# --- Controller 연결 -------------------------------------------------------
def test_승인된_action만_executor로_간다(config: Config, conn: sqlite3.Connection):
    """Approval Gate(Phase 14.1) 유지: 승인된 Action만 실행된다."""
    media_pk, creator_id = seed_candidate(conn)
    approved_id = enqueue_action(
        conn,
        run_id="r1",
        media_pk=media_pk,
        creator_id=creator_id,
        action_type=ActionType.LIKE,
        target_score=90,
        priority=90,
        executor="browser",
        dry_run=False,
    )
    other_pk, other_creator = seed_candidate(conn, code="BBB222", username="creator_b")
    enqueue_action(
        conn,
        run_id="r1",
        media_pk=other_pk,
        creator_id=other_creator,
        action_type=ActionType.LIKE,
        target_score=90,
        priority=90,
        executor="browser",
        dry_run=False,
    )
    approve_action(conn, approved_id)
    conn.commit()

    page = FakeActionPage()
    cfg = live_config(config)
    executor = BrowserExecutor(dry_run=False, config=cfg, conn=conn, page=page)
    summary = ActionController(cfg, conn, executor).run()

    assert summary.success == 1
    assert page.likes == 1
    assert summary.awaiting_approval == 1  # 승인하지 않은 Action은 남아 있다


def test_controller가_중단_상태를_halt로_처리한다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = seed_candidate(conn)
    action_id = enqueue_action(
        conn,
        run_id="r1",
        media_pk=media_pk,
        creator_id=creator_id,
        action_type=ActionType.LIKE,
        target_score=90,
        priority=90,
        executor="browser",
        dry_run=False,
    )
    approve_action(conn, action_id)
    conn.commit()

    cfg = live_config(config)
    page = FakeActionPage(stop_after_action=STOP_ACTION_BLOCKED)
    executor = BrowserExecutor(dry_run=False, config=cfg, conn=conn, page=page)
    summary = ActionController(cfg, conn, executor).run()

    assert summary.halted is True
    assert "ACTION_BLOCKED" in summary.halt_reason
    row = conn.execute(
        "SELECT status FROM action_queue WHERE action_id = ?", (action_id,)
    ).fetchone()
    assert row["status"] == ActionStatus.CANCELLED.value


# --- 금지 동작 ------------------------------------------------------------
def test_follow_dm_save_share_selector가_없다():
    """selector 상수에는 LIKE/COMMENT만 존재한다(금지 동작은 기능 자체가 없다)."""
    selectors = " ".join(
        selector
        for name in ("LIKE_BUTTON", "ALREADY_LIKED", "COMMENT_INPUT", "COMMENT_SUBMIT")
        for _key, selector in getattr(ASEL, name)
    ).lower()
    for banned in ("follow", "팔로우", "message", "메시지", "저장", "save", "공유", "share"):
        assert banned not in selectors, f"쓰기 selector에 금지 동작: {banned}"

    names = {name for name in dir(ASEL) if name.isupper()}
    assert names == {
        "LIKE_BUTTON",
        "ALREADY_LIKED",
        "COMMENT_INPUT",
        "COMMENT_SUBMIT",
        "ACTION_BLOCKED_TEXTS",
    }


def test_discovery_모듈에는_쓰기_selector가_없다():
    from pathlib import Path

    from targeting_agent.discovery import browser_selectors

    text = Path(browser_selectors.__file__).read_text(encoding="utf-8")
    for banned in ("좋아요", "Like", "댓글", "게시", "Post"):
        assert banned not in text, f"읽기 selector 파일에 쓰기 흔적: {banned}"
