"""Action Policy / 자동 승인 테스트 (Phase 18B.1)."""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.actions.policy import (
    ActionPolicy,
    AutoApproveResult,
    RunMode,
    auto_approve,
    pending_unapproved,
    recommended_for,
    usage_note,
)
from targeting_agent.core.config import Config
from targeting_agent.core.database import enqueue_action, fetch_executable_actions
from targeting_agent.core.models import ActionType

from .test_browser_executor import seed_candidate


def policy_of(config: Config) -> ActionPolicy:
    return ActionPolicy.from_config(config)


def autopilot_config(config: Config, **autopilot) -> Config:
    raw = {
        **config.raw,
        "autopilot": {"enabled": True, "max_approvals_per_run": 10, **autopilot},
    }
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


# --- 추천 규칙 -------------------------------------------------------------
@pytest.mark.parametrize(
    "score, expected",
    [
        (None, ()),
        (50.0, ()),
        (69.9, ()),                                  # 최소 기준 미달
        (74.0, ()),                                  # LIKE 기준 미달
        (75.0, (ActionType.LIKE,)),
        (81.9, (ActionType.LIKE,)),
        (82.0, (ActionType.LIKE, ActionType.COMMENT)),
        (95.0, (ActionType.LIKE, ActionType.COMMENT)),
    ],
)
def test_점수가_추천_action을_정한다(config: Config, score, expected):
    assert policy_of(config).recommend(score) == expected


def test_설정에서_action을_끄면_추천하지_않는다(config: Config):
    raw = {
        **config.raw,
        "actions": {**config.raw["actions"], "enable_comment": False},
    }
    policy = ActionPolicy.from_config(Config(raw=raw, path=config.path, base_dir=config.base_dir))

    assert policy.recommend(95.0) == (ActionType.LIKE,)


def test_임계값은_config에서만_읽는다(config: Config):
    policy = policy_of(config)

    assert policy.like_threshold == float(config.get("actions.require_score_for_like"))
    assert policy.comment_threshold == float(config.get("actions.require_score_for_comment"))
    assert policy.minimum_score == float(config.get("scoring.minimum_target_score"))


def test_설명_문구에_기준이_들어간다(config: Config):
    text = policy_of(config).explain(90.0)

    assert "Score 90" in text and "LIKE" in text and "COMMENT" in text


def test_추천_분포를_집계한다(config: Config):
    counts = recommended_for(policy_of(config), [None, 60.0, 76.0, 90.0, 95.0])

    assert counts == {"none": 2, "like": 1, "like_comment": 2}


# --- 모드 ------------------------------------------------------------------
def test_기본은_review_모드다(config: Config):
    assert RunMode.from_config(config) is RunMode.REVIEW


def test_autopilot을_켜면_autopilot_모드다(config: Config):
    assert RunMode.from_config(autopilot_config(config)) is RunMode.AUTOPILOT


# --- 자동 승인 -------------------------------------------------------------
def queue_action(
    conn: sqlite3.Connection,
    *,
    code: str,
    username: str,
    score: float,
    action_type: ActionType = ActionType.LIKE,
    comment_text: str | None = None,
) -> int:
    media_pk, creator_id = seed_candidate(conn, code=code, username=username)
    action_id = enqueue_action(
        conn,
        run_id="r1",
        media_pk=media_pk,
        creator_id=creator_id,
        action_type=action_type,
        target_score=score,
        priority=int(score),
        executor="browser",
        dry_run=True,
        comment_text=comment_text,
    )
    conn.commit()
    assert action_id is not None
    return int(action_id)


def test_review_모드는_아무것도_승인하지_않는다(config: Config, conn: sqlite3.Connection):
    queue_action(conn, code="AAA111", username="creator_a", score=95.0)

    result = auto_approve(conn, config)

    assert isinstance(result, AutoApproveResult)
    assert result.mode == RunMode.REVIEW.value
    assert result.approved == 0
    assert fetch_executable_actions(conn) == []  # Approval Gate 유지


def test_autopilot은_기준을_만족한_action만_승인한다(config: Config, conn: sqlite3.Connection):
    high = queue_action(conn, code="AAA111", username="creator_a", score=95.0)
    low = queue_action(conn, code="BBB222", username="creator_b", score=72.0)

    result = auto_approve(conn, autopilot_config(config))

    assert result.approved == 1
    assert result.approved_ids == [high]
    assert "below_threshold_like" in result.reasons
    executable = [int(row["action_id"]) for row in fetch_executable_actions(conn)]
    assert executable == [high]
    assert low not in executable


def test_댓글_텍스트가_없으면_comment_action을_승인하지_않는다(
    config: Config, conn: sqlite3.Connection
):
    queue_action(
        conn,
        code="AAA111",
        username="creator_a",
        score=95.0,
        action_type=ActionType.COMMENT,
    )

    result = auto_approve(conn, autopilot_config(config))

    assert result.approved == 0
    assert result.reasons.get("no_comment_text") == 1


def test_댓글_텍스트가_있으면_승인한다(config: Config, conn: sqlite3.Connection):
    queue_action(
        conn,
        code="AAA111",
        username="creator_a",
        score=95.0,
        action_type=ActionType.COMMENT,
        comment_text="저도 퇴근하고 가고 싶네요",
    )

    assert auto_approve(conn, autopilot_config(config)).approved == 1


def test_creator_미확인_후보는_승인하지_않는다(config: Config, conn: sqlite3.Connection):
    queue_action(conn, code="AAA111", username="unresolved:AAA111", score=95.0)

    result = auto_approve(conn, autopilot_config(config))

    assert result.approved == 0
    assert result.reasons.get("creator_unresolved") == 1


def test_실행당_승인_상한을_지킨다(config: Config, conn: sqlite3.Connection):
    for index in range(3):
        queue_action(conn, code=f"C{index:04d}", username=f"creator_{index}", score=95.0)

    result = auto_approve(conn, autopilot_config(config, max_approvals_per_run=2))

    assert result.approved == 2
    assert result.reasons.get("approval_cap") == 1


def test_이미_승인된_action은_다시_승인하지_않는다(config: Config, conn: sqlite3.Connection):
    queue_action(conn, code="AAA111", username="creator_a", score=95.0)
    cfg = autopilot_config(config)

    first = auto_approve(conn, cfg)
    second = auto_approve(conn, cfg)

    assert first.approved == 1
    assert second.approved == 0  # 승인 대기 목록에서 빠졌다
    assert pending_unapproved(conn) == []


def test_사용량_요약에_일일_한도가_보인다(config: Config, conn: sqlite3.Connection):
    text = usage_note(config, conn)

    assert "LIKE 0/" in text and "COMMENT 0/" in text
