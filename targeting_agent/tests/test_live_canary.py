"""Limited Live Canary 테스트 (Phase 18E).

여기 있는 테스트는 **실제 Instagram에 쓰는 것을 막는 장치**를 고정한다.
실제 Instagram에 접속하지 않는다 — Fake Executor와 메모리 DB만 쓴다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.canary.executor import (
    UNKNOWN_WRITE_STATE,
    CanaryViolation,
    LikeOnlyExecutor,
)
from targeting_agent.canary.runner import (
    APPROVED_BY_CANARY,
    OUTCOME_EXECUTED,
    OUTCOME_EXPIRED,
    OUTCOME_NO_BUDGET,
    OUTCOME_NO_CANDIDATES,
    OUTCOME_STOPPED,
    OUTCOME_TERMINAL,
    OUTCOME_TOO_SOON,
    live_override,
    run_canary,
)
from targeting_agent.canary.state import (
    ARMED,
    COMPLETED,
    MAX_LIKES_PER_DAY,
    MAX_LIKES_PER_RUN,
    MAX_TOTAL_LIKES,
    STOPPED_UNKNOWN_WRITE,
    STOPPED_VIOLATION,
    CanaryState,
    arm,
    load_state,
    save_state,
    state_path,
)
from targeting_agent.core.config import Config
from targeting_agent.core.exceptions import PlatformWarningError
from targeting_agent.core.models import ActionStatus, ActionType, ExecutionResult, QueuedAction

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# Fake — 실제 브라우저 대신
# ===========================================================================
class FakeInner:
    """BrowserExecutor 대역. 실제 Instagram에 접속하지 않는다."""

    name = "browser"

    def __init__(
        self,
        *,
        live: bool = True,
        like_ok: bool = True,
        persisted: bool = True,
        stop_reason: str = "",
    ) -> None:
        self.dry_run = not live
        self.live = live
        self.like_ok = like_ok
        self.persisted = persisted
        self.stop_reason = stop_reason
        self.like_calls: list[int] = []
        self.comment_calls: list[int] = []
        self.opened: list[str] = []
        self._page = self

    # BrowserExecutor 쪽 계약
    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        self.like_calls.append(action.action_id)
        if self.stop_reason:
            raise PlatformWarningError(self.stop_reason)
        if not self.like_ok:
            return ExecutionResult(
                success=False, status=ActionStatus.FAILED, error="like_not_confirmed",
                dry_run=self.dry_run,
            )
        return ExecutionResult(
            success=True, status=ActionStatus.SUCCESS, detail=f"LIKE {action.permalink}",
            dry_run=self.dry_run,
        )

    def execute_comment(self, action: QueuedAction) -> ExecutionResult:  # pragma: no cover
        self.comment_calls.append(action.action_id)
        return ExecutionResult(success=True, status=ActionStatus.SUCCESS, dry_run=self.dry_run)

    def validate_session(self) -> bool:
        return True

    def finalize(self) -> None:
        pass

    def health_check(self) -> dict:
        return {"executor": self.name, "dry_run": self.dry_run}

    def _media_url(self, action: QueuedAction) -> str:
        return action.permalink

    # ActionPage 쪽 계약(확인용)
    def open_media(self, url: str) -> None:
        self.opened.append(url)

    def already_liked(self) -> bool:
        return self.persisted


def _action(action_id: int = 1, action_type: ActionType = ActionType.LIKE) -> QueuedAction:
    return QueuedAction(
        action_id=action_id,
        media_pk=action_id,
        media_id=f"M{action_id}",
        permalink=f"https://www.instagram.com/reel/M{action_id}/",
        username="office_daily_kim",
        creator_id=action_id,
        action_type=action_type,
        target_score=90.0,
        priority=90,
    )


# ===========================================================================
# COMMENT 차단 — 가장 중요한 가드
# ===========================================================================
def test_comment는_실행되지_않고_위반으로_중단된다():
    """Queue에 승인된 COMMENT가 섞여 있어도 절대 나가면 안 된다."""
    inner = FakeInner()
    executor = LikeOnlyExecutor(inner)

    with pytest.raises(CanaryViolation):
        executor.execute_comment(_action(1, ActionType.COMMENT))

    assert inner.comment_calls == []  # 안쪽 Executor까지 가지도 않는다
    assert executor.comment_attempts == 1


def test_comment_action이_like경로로_들어와도_막는다():
    executor = LikeOnlyExecutor(FakeInner())

    with pytest.raises(CanaryViolation):
        executor.execute_like(_action(1, ActionType.COMMENT))


def test_like전용_executor는_comment를_지원하지_않는다고_밝힌다():
    executor = LikeOnlyExecutor(FakeInner())

    assert executor.supports_comment is False
    assert executor.supports_like is True


# ===========================================================================
# 눌린 것 확인 (Execution Ground Truth)
# ===========================================================================
def test_새로고침_후에도_남아_있어야_확인으로_센다():
    inner = FakeInner(persisted=True)
    executor = LikeOnlyExecutor(inner)

    result = executor.execute_like(_action(1))

    assert result.status is ActionStatus.SUCCESS
    assert "verified" in result.detail
    assert executor.confirmed == 1
    assert inner.opened  # 새로고침해서 다시 봤다


def test_새로고침했더니_없으면_unknown_write_state다():
    """click은 됐는데 남지 않았다 — 다시 누르면 취소되므로 건드리지 않는다."""
    inner = FakeInner(persisted=False)
    executor = LikeOnlyExecutor(inner)

    result = executor.execute_like(_action(1))

    assert result.status is ActionStatus.FAILED
    assert result.error == UNKNOWN_WRITE_STATE
    assert executor.unknown_write == 1
    assert executor.confirmed == 0
    assert len(inner.like_calls) == 1  # 재시도하지 않았다


def test_dry_run이면_확인_단계를_거치지_않는다():
    inner = FakeInner(live=False)
    executor = LikeOnlyExecutor(inner)

    result = executor.execute_like(_action(1))

    assert result.status is ActionStatus.SUCCESS
    assert inner.opened == []  # 브라우저를 열지 않았다
    assert executor.confirmed == 0


def test_click_자체가_실패하면_확인하지_않는다():
    inner = FakeInner(like_ok=False)
    executor = LikeOnlyExecutor(inner)

    result = executor.execute_like(_action(1))

    assert result.status is ActionStatus.FAILED
    assert inner.opened == []


# ===========================================================================
# 설정 — 원본은 끝까지 안전해야 한다
# ===========================================================================
def test_runtime_사본만_live가_되고_원본은_그대로다(config: Config):
    """프로세스가 죽어도 config.yaml이 LIVE로 남으면 안 된다."""
    runtime = live_override(config)

    assert runtime.get("browser_executor.mode") == "LIVE"
    assert runtime.dry_run is False
    # 원본은 손대지 않았다.
    assert config.get("browser_executor.mode") == "DRY_RUN"
    assert config.dry_run is True


def test_runtime_사본은_comment를_끈다(config: Config):
    runtime = live_override(config)

    assert runtime.get("actions.enable_comment") is False
    assert config.get("actions.enable_comment") is True  # 원본 불변


def test_runtime_사본을_바꿔도_원본_dict가_공유되지_않는다(config: Config):
    runtime = live_override(config)

    runtime.raw["actions"]["daily_limits"]["likes"] = 999

    assert config.get("actions.daily_limits.likes") != 999


# ===========================================================================
# 한도 — 코드에서 fail closed
# ===========================================================================
def test_상한은_코드에_박혀_있고_설정으로_키울_수_없다():
    state = CanaryState(max_total=999, max_per_day=999, max_per_run=999)

    assert state.remaining_total() == MAX_TOTAL_LIKES
    assert state.remaining_today() == MAX_LIKES_PER_DAY
    assert state.budget_for_run() == MAX_LIKES_PER_RUN


def test_총량을_다_쓰면_예산이_0이다():
    state = CanaryState(total_live_likes=MAX_TOTAL_LIKES)

    assert state.budget_for_run() == 0


def test_오늘치를_다_쓰면_예산이_0이다():
    state = CanaryState(today_live_likes=MAX_LIKES_PER_DAY, today_date="2026-10-07")

    state.roll_day(now=NOW)

    assert state.budget_for_run() == 0


def test_날짜가_바뀌면_오늘치가_초기화된다():
    state = CanaryState(today_live_likes=3, today_date="2026-10-06", total_live_likes=3)

    state.roll_day(now=NOW)

    assert state.today_live_likes == 0
    assert state.remaining_total() == MAX_TOTAL_LIKES - 3  # 총량은 그대로 남는다


def test_예산은_셋_중_가장_작은_값이다():
    state = CanaryState(total_live_likes=5)  # 총량 1건 남음

    assert state.remaining_total() == 1
    assert state.budget_for_run() == 1  # per_run(2)보다 작다


def test_8시간이_지나지_않으면_실행하지_않는다():
    state = CanaryState(last_run_at=(NOW - timedelta(hours=3)).isoformat())

    assert state.interval_ok(now=NOW) is False
    assert state.interval_ok(now=NOW + timedelta(hours=6)) is True


def test_48시간이_지나면_만료다():
    state = CanaryState(ends_at=(NOW - timedelta(minutes=1)).isoformat())

    assert state.is_expired(now=NOW) is True
    assert state.is_expired(now=NOW - timedelta(hours=1)) is False


def test_arm은_48시간_창을_연다():
    state = arm(CanaryState(), now=NOW)

    assert state.status == ARMED
    assert state.canary_id.startswith("canary-")
    ends = datetime.fromisoformat(state.ends_at)
    assert (ends - NOW) == timedelta(hours=48)


# ===========================================================================
# 상태 파일 — 마지막 방어선
# ===========================================================================
def test_상태는_파일로_살아남는다(tmp_path):
    path = state_path(tmp_path)
    state = arm(CanaryState(), now=NOW)
    state.record_like(42, now=NOW)

    save_state(path, state)
    restored = load_state(path)

    assert restored.total_live_likes == 1
    assert restored.review_backlog == [42]
    assert restored.canary_id == state.canary_id


def test_상태파일이_깨졌으면_처음부터가_아니라_막는다(tmp_path):
    """읽을 수 없는 상태를 '처음'으로 해석하면 한도를 다시 쓰게 된다."""
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 깨진 json", encoding="utf-8")

    restored = load_state(path)

    assert restored.terminal is True
    assert restored.stop_reason == "state_file_unreadable"


def test_확인된_건만_상태에_기록한다():
    state = CanaryState()

    state.record_like(7, now=NOW)

    assert state.total_live_likes == 1
    assert state.today_live_likes == 1
    assert 7 in state.review_backlog


# ===========================================================================
# Runner — 전체 흐름
# ===========================================================================
def _seed(conn: sqlite3.Connection, score: float = 90.0, count: int = 3) -> None:
    for index in range(count):
        cursor = conn.execute(
            "INSERT INTO creators (username, followers, status, first_seen_at, updated_at) "
            "VALUES (?, 5000, 'NEW', '2026-10-01', '2026-10-01')",
            (f"user{index}",),
        )
        creator_id = int(cursor.lastrowid)
        url = f"https://www.instagram.com/reel/CODE{index}/"
        cursor = conn.execute(
            """INSERT INTO candidate_media
               (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
                source, status, target_score, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, 'REEL', '퇴근길', '[]', 'instagram_browser_search',
                       'QUEUED', ?, '2026-10-01', '2026-10-01')""",
            (f"CODE{index}", creator_id, url, url, score),
        )
        media_pk = int(cursor.lastrowid)
        conn.execute(
            """INSERT INTO action_queue
               (run_id, media_pk, creator_id, action_type, target_score, priority, status,
                executor, dry_run, created_at, updated_at)
               VALUES ('run-seed', ?, ?, 'LIKE', ?, 90, 'PENDING', 'browser', 1,
                       '2026-10-01', '2026-10-01')""",
            (media_pk, creator_id, score),
        )
    conn.commit()


@pytest.fixture()
def canary_config(config: Config, tmp_path) -> Config:
    raw = {key: (dict(value) if isinstance(value, dict) else value) for key, value in config.raw.items()}
    raw["paths"] = {**raw["paths"], "data_dir": str(tmp_path)}
    return Config(raw=raw, path=config.path, base_dir=tmp_path)


def test_canary는_like만_승인한다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn)
    conn.execute(
        """INSERT INTO action_queue
           (run_id, media_pk, creator_id, action_type, comment_text, target_score, priority,
            status, executor, dry_run, created_at, updated_at)
           VALUES ('run-seed', 1, 1, 'COMMENT', '퇴근길 좋네요', 95.0, 95, 'PENDING',
                   'browser', 1, '2026-10-01', '2026-10-01')"""
    )
    conn.commit()
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(inner)
    )

    assert result.outcome == OUTCOME_EXECUTED
    assert inner.comment_calls == []
    approved = conn.execute(
        "SELECT action_type, approved_by FROM action_queue WHERE approved_at IS NOT NULL"
    ).fetchall()
    assert approved and all(row[0] == "LIKE" for row in approved)
    assert all(row[1] == APPROVED_BY_CANARY for row in approved)


def test_canary_승인은_운영자_승인과_구분된다(conn: sqlite3.Connection, canary_config: Config):
    """Agent의 자동 승인을 사람 승인으로 위장하지 않는다."""
    _seed(conn, count=1)

    run_canary(canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(FakeInner()))

    row = conn.execute(
        "SELECT approved_by FROM action_queue WHERE approved_at IS NOT NULL"
    ).fetchone()
    assert row[0] == APPROVED_BY_CANARY
    assert row[0] != "operator"


def test_한_run에_2건까지만_누른다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn, count=5)
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(inner)
    )

    assert result.budget == MAX_LIKES_PER_RUN
    assert len(inner.like_calls) <= MAX_LIKES_PER_RUN


def test_임계값_미만_후보는_고르지_않는다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn, score=74.99, count=3)
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(inner)
    )

    assert result.outcome == OUTCOME_NO_CANDIDATES
    assert inner.like_calls == []
    assert any("낮추지 않고" in message for message in result.messages)


def test_임계값_경계값은_고른다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn, score=75.0, count=1)

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(FakeInner())
    )

    assert result.outcome == OUTCOME_EXECUTED


def test_이미_실제로_누른_게시물은_다시_고르지_않는다(
    conn: sqlite3.Connection, canary_config: Config
):
    _seed(conn, count=1)
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_type, executor, dry_run, success, executed_at)
           VALUES (1, 1, 'LIKE', 'browser', 0, 1, '2026-10-01T00:00:00+00:00')"""
    )
    conn.commit()
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(inner)
    )

    assert result.outcome == OUTCOME_NO_CANDIDATES
    assert inner.like_calls == []


def test_creator가_미확인이면_고르지_않는다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn, count=1)
    conn.execute("UPDATE creators SET username = 'unresolved:CODE0' WHERE creator_id = 1")
    conn.commit()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(FakeInner())
    )

    assert result.outcome == OUTCOME_NO_CANDIDATES


def test_총량을_다_쓰면_아무것도_하지_않는다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn)
    state = arm(CanaryState(), now=NOW)
    state.total_live_likes = MAX_TOTAL_LIKES
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW,
        executor=LikeOnlyExecutor(inner), state=state,
    )

    assert result.outcome == OUTCOME_NO_BUDGET
    assert inner.like_calls == []


def test_8시간_전이면_실행하지_않는다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn)
    state = arm(CanaryState(), now=NOW - timedelta(hours=10))
    state.last_run_at = (NOW - timedelta(hours=2)).isoformat()
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW,
        executor=LikeOnlyExecutor(inner), state=state,
    )

    assert result.outcome == OUTCOME_TOO_SOON
    assert inner.like_calls == []


def test_48시간이_지나면_완료로_닫는다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn)
    state = arm(CanaryState(), now=NOW - timedelta(hours=49))
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW,
        executor=LikeOnlyExecutor(inner), state=state,
    )

    assert result.outcome == OUTCOME_EXPIRED
    assert state.status == COMPLETED
    assert inner.like_calls == []


def test_종료된_canary는_scheduler가_불러도_no_op이다(
    conn: sqlite3.Connection, canary_config: Config
):
    """Task 삭제에 실패해도 추가 LIKE가 생기면 안 된다 — 상태가 최종 방어선이다."""
    _seed(conn)
    state = arm(CanaryState(), now=NOW)
    state.stop(COMPLETED, "total_reached", now=NOW)
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW,
        executor=LikeOnlyExecutor(inner), state=state,
    )

    assert result.outcome == OUTCOME_TERMINAL
    assert inner.like_calls == []


def test_unknown_write가_생기면_canary를_멈춘다(
    conn: sqlite3.Connection, canary_config: Config
):
    _seed(conn, count=3)
    state = arm(CanaryState(), now=NOW)
    inner = FakeInner(persisted=False)

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW,
        executor=LikeOnlyExecutor(inner), state=state,
    )

    assert result.outcome == OUTCOME_STOPPED
    assert state.status == STOPPED_UNKNOWN_WRITE


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("ACTION_BLOCKED:LIKE x", "STOPPED_ACTION_BLOCK"),
        ("CHALLENGE:LIKE x", "STOPPED_CHALLENGE"),
        ("PLATFORM_WARNING:LIKE x", "STOPPED_WARNING"),
        ("LOGIN_REQUIRED:LIKE x", "STOPPED_LOGIN"),
    ],
)
def test_플랫폼_신호는_즉시_중단한다(
    conn: sqlite3.Connection, canary_config: Config, reason: str, expected: str
):
    """우회하지 않는다 — 멈춘다."""
    _seed(conn, count=3)
    state = arm(CanaryState(), now=NOW)
    inner = FakeInner(stop_reason=reason)

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW,
        executor=LikeOnlyExecutor(inner), state=state,
    )

    assert result.outcome == OUTCOME_STOPPED
    assert state.status == expected
    assert state.terminal is True


def test_comment_위반이면_canary를_멈춘다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn, count=1)
    state = arm(CanaryState(), now=NOW)

    class CommentingExecutor(LikeOnlyExecutor):
        def execute_like(self, action):  # noqa: D102
            raise CanaryViolation("CRITICAL_CANARY_VIOLATION: forced")

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW,
        executor=CommentingExecutor(FakeInner()), state=state,
    )

    assert result.outcome == OUTCOME_STOPPED
    assert state.status == STOPPED_VIOLATION


def test_리허설은_실제로_누르지_않는다(conn: sqlite3.Connection, canary_config: Config):
    _seed(conn, count=2)
    state = arm(CanaryState(), now=NOW)

    result = run_canary(
        canary_config, conn=conn, live=False, now=NOW,
        executor=LikeOnlyExecutor(FakeInner(live=False)), state=state,
    )

    assert result.live is False
    assert state.total_live_likes == 0
    real = conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE dry_run = 0"
    ).fetchone()[0]
    assert real == 0


def test_실행_후_상태가_파일에_남는다(conn: sqlite3.Connection, canary_config: Config, tmp_path):
    _seed(conn, count=2)

    run_canary(canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(FakeInner()))

    restored = load_state(state_path(tmp_path))
    assert restored.runs == 1
    assert restored.canary_id


def test_열어본_적_없는_주소에는_좋아요를_누르지_않는다(
    conn: sqlite3.Connection, canary_config: Config
):
    """canonical_url은 그 게시물을 실제로 열어 봤을 때만 채워진다.

    CSV로 들여온 샘플처럼 한 번도 확인한 적 없는 주소에 실제 동작을 하면
    존재하지 않는 페이지를 훑는 꼴이 된다.
    """
    _seed(conn, count=1)
    conn.execute("UPDATE candidate_media SET canonical_url = NULL WHERE media_pk = 1")
    conn.commit()
    inner = FakeInner()

    result = run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(inner)
    )

    assert result.outcome == OUTCOME_NO_CANDIDATES
    assert inner.like_calls == []


def test_리허설은_48시간_창을_시작하지_않는다(
    conn: sqlite3.Connection, canary_config: Config, tmp_path
):
    """실제로 누르지도 않았는데 시계가 돌면 정작 쓸 수 있을 때 기간이 줄어든다."""
    _seed(conn, count=1)

    run_canary(
        canary_config, conn=conn, live=False, now=NOW,
        executor=LikeOnlyExecutor(FakeInner(live=False)),
    )

    assert not state_path(tmp_path).exists()


def test_실제_run은_48시간_창을_시작한다(
    conn: sqlite3.Connection, canary_config: Config, tmp_path
):
    _seed(conn, count=1)

    run_canary(
        canary_config, conn=conn, live=True, now=NOW, executor=LikeOnlyExecutor(FakeInner())
    )

    saved = load_state(state_path(tmp_path))
    assert saved.started_at
    assert saved.ends_at
