"""Action Queue / Rate Limiter / Executor 테스트."""
from __future__ import annotations

import pytest

from targeting_agent.actions.executors import create_executor
from targeting_agent.actions.executors.browser import BrowserExecutor
from targeting_agent.actions.executors.manual import ManualExecutor
from targeting_agent.actions.executors.official_api import OfficialAPIExecutor
from targeting_agent.actions.queue import ActionQueueBuilder
from targeting_agent.actions.rate_limiter import RateLimiter
from targeting_agent.core.config import Config
from targeting_agent.core.database import insert_candidate, record_interaction
from targeting_agent.core.exceptions import ConfigError
from targeting_agent.core.models import ActionStatus, ActionType, MediaStatus, QueuedAction


def _media_row(conn, candidate):
    media_pk = insert_candidate(conn, candidate)
    conn.commit()
    return dict(conn.execute("SELECT * FROM candidate_media WHERE media_pk = ?", (media_pk,)).fetchone())


def _action(action_type=ActionType.LIKE, comment_text=None) -> QueuedAction:
    return QueuedAction(
        action_id=1, media_pk=1, media_id="M1", permalink="https://www.instagram.com/reel/M1/",
        username="tester", creator_id=1, action_type=action_type, target_score=90.0,
        priority=90, comment_text=comment_text,
    )


# --- Action Queue -------------------------------------------------------
def test_queue_thresholds(config, conn, sample_candidate) -> None:
    row = _media_row(conn, sample_candidate)
    builder = ActionQueueBuilder(config, run_id="r1")

    high = builder.build(conn, row, 95.0, comment_text="퇴근 분위기 좋네요")
    assert (high.likes, high.comments) == (1, 1)
    assert conn.execute("SELECT status FROM candidate_media WHERE media_pk=?", (row["media_pk"],)).fetchone()[0] == MediaStatus.QUEUED.value


def test_queue_like_only_and_skip(config, conn, sample_candidate) -> None:
    row = _media_row(conn, sample_candidate)
    builder = ActionQueueBuilder(config, run_id="r1")

    mid = builder.build(conn, row, 78.0, comment_text="퇴근 분위기 좋네요")
    assert (mid.likes, mid.comments) == (1, 0)      # COMMENT 임계값(82) 미달

    other = dict(row)
    other["media_pk"] = insert_candidate(
        conn, type(sample_candidate)(media_id="M2", permalink="p", username="other", followers=3000)
    )
    low = builder.build(conn, other, 50.0)
    assert (low.likes, low.comments, low.skipped) == (0, 0, 1)


def test_queue_duplicate_action_detected(config, conn, sample_candidate) -> None:
    row = _media_row(conn, sample_candidate)
    builder = ActionQueueBuilder(config, run_id="r1")
    builder.build(conn, row, 95.0, comment_text="퇴근 분위기 좋네요")
    again = builder.build(conn, row, 95.0, comment_text="퇴근 분위기 좋네요")
    assert again.duplicates == 2 and again.total == 0


# --- Rate Limiter -------------------------------------------------------
def test_rate_limiter_daily_limit(config, conn, sample_candidate) -> None:
    raw = {**config.raw}
    raw["actions"] = {**raw["actions"], "daily_limits": {**raw["actions"]["daily_limits"], "likes": 1}}
    limited = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    row = _media_row(conn, sample_candidate)
    limiter = RateLimiter(limited, conn, dry_run=True)
    assert limiter.check(ActionType.LIKE, 1, row["media_pk"]).allowed is True

    record_interaction(
        conn, media_pk=row["media_pk"], creator_id=1, action_id=None,
        action_type=ActionType.LIKE, executor="manual", dry_run=True, success=True,
    )
    conn.commit()
    decision = limiter.check(ActionType.LIKE, 1, row["media_pk"] )
    assert decision.allowed is False and "daily_limit" in decision.reason


def test_rate_limiter_allows_comment_after_like_same_post(config, conn, sample_candidate) -> None:
    """같은 게시물에 LIKE 후 COMMENT는 설계상 정상 흐름이므로 허용되어야 한다."""
    row = _media_row(conn, sample_candidate)
    record_interaction(
        conn, media_pk=row["media_pk"], creator_id=1, action_id=None,
        action_type=ActionType.LIKE, executor="manual", dry_run=True, success=True,
    )
    conn.commit()
    limiter = RateLimiter(config, conn, dry_run=True)
    assert limiter.check(ActionType.COMMENT, 1, row["media_pk"]).allowed is True
    # 같은 종류(LIKE) 재실행은 차단
    assert limiter.check(ActionType.LIKE, 1, row["media_pk"]).reason == "already_interacted_media"


def test_rate_limiter_creator_limit_other_post(config, conn, sample_candidate) -> None:
    row = _media_row(conn, sample_candidate)
    other_pk = insert_candidate(
        conn, type(sample_candidate)(media_id="M9", permalink="p", username=sample_candidate.username)
    )
    conn.commit()
    record_interaction(
        conn, media_pk=row["media_pk"], creator_id=1, action_id=None,
        action_type=ActionType.LIKE, executor="manual", dry_run=True, success=True,
    )
    conn.commit()

    limiter = RateLimiter(config, conn, dry_run=True)
    decision = limiter.check(ActionType.LIKE, 1, other_pk)
    assert decision.allowed is False
    assert "creator" in decision.reason


def test_rate_limiter_dry_run_isolated_from_real(config, conn, sample_candidate) -> None:
    """Dry Run 기록이 실제 실행 한도를 소모하지 않아야 한다."""
    row = _media_row(conn, sample_candidate)
    record_interaction(
        conn, media_pk=row["media_pk"], creator_id=1, action_id=None,
        action_type=ActionType.LIKE, executor="manual", dry_run=True, success=True,
    )
    conn.commit()
    assert RateLimiter(config, conn, dry_run=True).used_today(ActionType.LIKE) == 1
    assert RateLimiter(config, conn, dry_run=False).used_today(ActionType.LIKE) == 0


def test_rate_limiter_max_actions_per_run(config, conn, sample_candidate) -> None:
    row = _media_row(conn, sample_candidate)
    limiter = RateLimiter(config, conn, dry_run=True)
    limiter.max_actions_per_run = 1
    limiter.record_executed()
    assert "max_actions_per_run" in limiter.check(ActionType.LIKE, 1, row["media_pk"]).reason


# --- Executors ----------------------------------------------------------
def test_manual_executor_writes_export(tmp_path) -> None:
    executor = ManualExecutor(export_dir=tmp_path, run_id="r1", dry_run=True)
    assert executor.validate_session() is True

    like = executor.execute_like(_action())
    comment = executor.execute_comment(_action(ActionType.COMMENT, "퇴근 분위기 좋네요"))
    missing = executor.execute_comment(_action(ActionType.COMMENT))
    executor.finalize()

    assert like.status is ActionStatus.SUCCESS and comment.status is ActionStatus.SUCCESS
    assert missing.status is ActionStatus.SKIPPED
    content = executor.export_path.read_text(encoding="utf-8-sig")
    assert "LIKE" in content and "퇴근 분위기 좋네요" in content


def test_stub_executors_do_not_act() -> None:
    browser = BrowserExecutor(dry_run=True)
    assert browser.validate_session() is False
    assert browser.execute_like(_action()).status is ActionStatus.SKIPPED

    official = OfficialAPIExecutor(dry_run=True)
    assert official.execute_comment(_action(ActionType.COMMENT, "x")).status is ActionStatus.SKIPPED
    assert official.health_check()["supports_like"] is False


def test_create_executor_factory(tmp_path) -> None:
    assert isinstance(create_executor("manual", export_dir=tmp_path, run_id="r", dry_run=True), ManualExecutor)
    assert isinstance(create_executor("browser", export_dir=tmp_path, run_id="r", dry_run=True), BrowserExecutor)
    with pytest.raises(ConfigError):
        create_executor("unknown", export_dir=tmp_path, run_id="r", dry_run=True)
