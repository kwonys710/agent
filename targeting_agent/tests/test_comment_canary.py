"""COMMENT Live Canary 테스트 (Phase 19M).

실제 Instagram에 댓글을 달지 않는다 — 모든 경로는 FakePage로 검증한다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import pytest

from targeting_agent.actions.activity import observe
from targeting_agent.actions.executors.browser_page import (
    COMMENT_CONFIRMED,
    COMMENT_NOT_SENT,
    COMMENT_UNKNOWN_STATE,
    CommentResult,
)
from targeting_agent.canary.comment_executor import (
    HALT_UNKNOWN,
    SKIP_CAP,
    SKIP_LIKE,
    build_comment_only_executor,
)
from targeting_agent.canary.comment_runner import (
    OUTCOME_BLOCKED_ACTIVITY,
    OUTCOME_DISABLED,
    OUTCOME_DONE,
    OUTCOME_NO_CANDIDATES,
    OUTCOME_STOPPED,
    comment_live_override,
    eligible_comments,
    format_result,
    run_comment_canary,
)
from targeting_agent.core.config import Config
from targeting_agent.core.database import enqueue_action
from targeting_agent.core.models import ActionType

from .test_browser_executor import FakeActionPage
from .test_engagement_activity import add_draft, scored_candidate

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


class CommentPage(FakeActionPage):
    """post_comment까지 흉내내는 Fake. 상태를 지정할 수 있다."""

    def __init__(self, state: str = COMMENT_CONFIRMED, detail: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.detail = detail
        self.posts: list[str] = []

    def post_comment(self, text: str) -> CommentResult:
        self.posts.append(text)
        return CommentResult(self.state, text=text if self.state == COMMENT_CONFIRMED else "",
                             detail=self.detail)


def canary_config(config: Config, *, enabled: bool = True, limit: int = 1) -> Config:
    raw = {
        **config.raw,
        "executor": {**config.raw.get("executor", {}), "mode": "browser"},
        "canary": {
            "comment": {
                "enabled": enabled,
                "max_comments_per_run": limit,
                "lock": {"enabled": False},
            }
        },
    }
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def queue_comment(
    conn: sqlite3.Connection,
    *,
    code: str = "AAA111",
    username: str = "creator_a",
    score: float = 90.0,
    text: str = "저도 퇴근하고 한강 뛰러 가고 싶네요",
    caption: str = "퇴근 후 한강 러닝 #직장인 #퇴근 #러닝",
) -> int:
    media_pk, creator_id = scored_candidate(
        conn, code=code, username=username, score=score, caption=caption
    )
    conn.execute(
        "UPDATE candidate_media SET canonical_url = ? WHERE media_pk = ?",
        (f"https://www.instagram.com/reel/{code}/", media_pk),
    )
    add_draft(conn, media_pk, text)
    action_id = enqueue_action(
        conn,
        run_id="r1",
        media_pk=media_pk,
        creator_id=creator_id,
        action_type=ActionType.COMMENT,
        target_score=score,
        priority=int(score),
        executor="browser",
        dry_run=False,
        comment_text=text,
    )
    conn.commit()
    return int(action_id)


# --- 게이트 ----------------------------------------------------------------
def test_설정이_꺼져_있으면_아무것도_하지_않는다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn)
    page = CommentPage()

    result = run_comment_canary(canary_config(config, enabled=False), conn=conn, now=NOW, page=page)

    assert result.outcome == OUTCOME_DISABLED
    assert result.confirmed == 0
    assert page.posts == []


def test_리허설은_설정이_꺼져_있어도_돌지만_게시하지_않는다(
    config: Config, conn: sqlite3.Connection
):
    queue_comment(conn)
    page = CommentPage()

    result = run_comment_canary(
        canary_config(config, enabled=False), conn=conn, live=False, now=NOW, page=page
    )

    assert result.outcome == OUTCOME_DONE
    assert result.live is False
    assert page.posts == []  # 브라우저 게시 0건
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM interactions WHERE dry_run = 0"
    ).fetchone()["n"] == 0


@pytest.mark.parametrize("signal", ["PLATFORM_WARNING", "ACTION_BLOCKED", "CHALLENGE"])
def test_활동_상태가_normal이_아니면_하지_않는다(
    config: Config, conn: sqlite3.Connection, signal: str
):
    queue_comment(conn)
    observe(conn, signal, now=NOW)
    page = CommentPage()

    result = run_comment_canary(canary_config(config), conn=conn, now=NOW, page=page)

    assert result.outcome == OUTCOME_BLOCKED_ACTIVITY
    assert page.posts == []


def test_자격_후보가_없으면_정상_종료한다(config: Config, conn: sqlite3.Connection):
    page = CommentPage()

    result = run_comment_canary(canary_config(config), conn=conn, now=NOW, page=page)

    assert result.outcome == OUTCOME_NO_CANDIDATES
    assert "낮추지 않고" in " ".join(result.messages)
    assert page.posts == []


def test_합성_데이터에는_실제_댓글을_달지_않는다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn, code="SAMPLE001", username="sample_creator")
    page = CommentPage()

    result = run_comment_canary(canary_config(config), conn=conn, now=NOW, page=page)

    assert result.outcome == OUTCOME_NO_CANDIDATES
    assert page.posts == []


def test_caption이_없으면_후보가_아니다(config: Config, conn: sqlite3.Connection):
    action_id = queue_comment(conn, caption="x")
    conn.execute("UPDATE candidate_media SET caption = '' ")
    conn.commit()

    assert eligible_comments(conn, canary_config(config), limit=1) == []
    assert action_id  # 큐에는 남아 있다(기준만 통과하지 못했다)


# --- 실제 경로 --------------------------------------------------------------
def test_확인되면_한_건만_게시한다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn, code="AAA111", username="creator_a")
    queue_comment(conn, code="BBB222", username="creator_b")
    page = CommentPage(state=COMMENT_CONFIRMED)

    result = run_comment_canary(canary_config(config, limit=1), conn=conn, now=NOW, page=page)

    assert result.outcome == OUTCOME_DONE
    assert result.live is True
    assert result.confirmed == 1
    assert len(page.posts) == 1  # 상한을 코드에서 지킨다
    assert result.posted == page.posts
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM interactions WHERE action_type = 'COMMENT' AND dry_run = 0"
    ).fetchone()
    assert row["n"] == 1


def test_확인_불가면_즉시_중단하고_재시도하지_않는다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn, code="AAA111", username="creator_a")
    queue_comment(conn, code="BBB222", username="creator_b")
    page = CommentPage(state=COMMENT_UNKNOWN_STATE, detail="not_in_comment_list")

    result = run_comment_canary(canary_config(config, limit=2), conn=conn, now=NOW, page=page)

    assert result.outcome == OUTCOME_STOPPED
    assert HALT_UNKNOWN in result.stop_reason
    assert result.unknown == 1
    assert len(page.posts) == 1  # 한 번만 시도한다
    assert "다시 보내지 않습니다" in " ".join(result.messages)


def test_미게시는_실패로_남기고_계속_시도하지_않는다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn)
    page = CommentPage(state=COMMENT_NOT_SENT, detail="input_not_found")

    result = run_comment_canary(canary_config(config), conn=conn, now=NOW, page=page)

    assert result.confirmed == 0
    assert result.not_sent == 1
    assert result.outcome != OUTCOME_DONE


def test_같은_creator에게_두_번_달지_않는다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn, code="AAA111", username="creator_a")
    queue_comment(conn, code="AAA222", username="creator_a")  # 같은 Creator

    rows = eligible_comments(conn, canary_config(config, limit=2), limit=2)

    assert len(rows) == 1


def test_like는_이_run에서_실행되지_않는다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = scored_candidate(conn)
    like_id = enqueue_action(
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
    conn.commit()
    page = CommentPage()
    runtime = comment_live_override(canary_config(config), budget=1)
    executor = build_comment_only_executor(runtime, conn, run_id="t", page=page, max_comments=1)

    from targeting_agent.core.models import ActionStatus, QueuedAction

    outcome = executor.execute_like(
        QueuedAction(
            action_id=int(like_id),
            media_pk=media_pk,
            media_id="AAA111",
            permalink="https://www.instagram.com/reel/AAA111/",
            username="creator_a",
            creator_id=creator_id,
            action_type=ActionType.LIKE,
            target_score=90.0,
            priority=90,
            status=ActionStatus.PENDING,
            executor="browser",
        )
    )

    assert outcome.detail == SKIP_LIKE
    assert page.likes == 0


def test_상한에_도달하면_건너뛴다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn)
    page = CommentPage()
    runtime = comment_live_override(canary_config(config), budget=1)
    executor = build_comment_only_executor(runtime, conn, run_id="t", page=page, max_comments=0)

    from targeting_agent.core.models import ActionStatus, QueuedAction

    outcome = executor.execute_comment(
        QueuedAction(
            action_id=1,
            media_pk=1,
            media_id="AAA111",
            permalink="https://www.instagram.com/reel/AAA111/",
            username="creator_a",
            creator_id=1,
            action_type=ActionType.COMMENT,
            target_score=90.0,
            priority=90,
            status=ActionStatus.PENDING,
            comment_text="저도 퇴근하고 뛰고 싶네요",
            executor="browser",
        )
    )

    assert outcome.detail == SKIP_CAP
    assert page.posts == []


# --- 설정 보호 --------------------------------------------------------------
def test_live는_메모리_사본에서만_켜진다(config: Config, conn: sqlite3.Connection):
    base = canary_config(config)

    runtime = comment_live_override(base, budget=1)

    assert runtime.get("browser_executor.mode") == "LIVE"
    assert runtime.dry_run is False
    assert runtime.get("actions.enable_like") is False
    # 원본은 그대로다
    assert base.get("browser_executor.mode") == "DRY_RUN"
    assert base.dry_run is True


def test_실행_후에도_config_파일은_dry_run이다(config: Config, conn: sqlite3.Connection):
    from pathlib import Path

    queue_comment(conn)
    before = Path(config.path).read_text(encoding="utf-8")

    run_comment_canary(canary_config(config), conn=conn, now=NOW, page=CommentPage())

    assert Path(config.path).read_text(encoding="utf-8") == before
    assert 'mode: "DRY_RUN"' in before


def test_임계값을_낮추지_않는다(config: Config, conn: sqlite3.Connection):
    """기준 미달 후보는 Canary를 채우려고 끌어오지 않는다."""
    queue_comment(conn, score=70.0)  # COMMENT 기준(82) 미달

    rows = eligible_comments(conn, canary_config(config), limit=1)

    assert rows == []


def test_요약에_결과가_보인다(config: Config, conn: sqlite3.Connection):
    queue_comment(conn)

    result = run_comment_canary(canary_config(config), conn=conn, now=NOW, page=CommentPage())
    text = format_result(result)

    assert "COMMENT Live Canary" in text
    assert "게시 확인" in text
