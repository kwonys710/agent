"""Query Expansion / Comment Engagement / Activity Controller / Action Mix 테스트
(Phase 19C·19F·19H·19I).

Instagram 접속·쓰기 없음. Claude 호출 없음(모든 확장은 결정론적 조합).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.actions.activity import (
    ActivityState,
    effective_config,
    observe,
    reset,
)
from targeting_agent.actions.activity import status as activity_status
from targeting_agent.actions.policy import (
    COMMENT_BELOW_THRESHOLD,
    COMMENT_NO_CAPTION,
    COMMENT_QUALITY_FAIL,
    COMMENT_RECENT_SAME_CREATOR,
    ActionPolicy,
    comment_eligibility,
    decide_action_mix,
)
from targeting_agent.core.config import Config
from targeting_agent.core.database import record_interaction, save_comment_draft
from targeting_agent.core.models import ActionType
from targeting_agent.discovery.query_bank import (
    ORIGIN_EXPANSION,
    QueryStatus,
    get_query,
    seed_bank,
)
from targeting_agent.discovery.query_expansion import (
    STATUS_NOT_ENOUGH_DATA,
    STATUS_OK,
    STATUS_SKIPPED,
    combine,
    expand_queries,
    expansion_summary,
)

from .test_browser_executor import seed_candidate

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def scored_candidate(
    conn: sqlite3.Connection,
    *,
    code: str = "AAA111",
    username: str = "creator_a",
    score: float = 90.0,
    caption: str = "퇴근 후 한강 러닝 #직장인 #퇴근 #러닝",
    hashtags: tuple[str, ...] = ("직장인", "퇴근", "러닝"),
    topics: tuple[str, ...] = ("퇴근", "러닝"),
) -> tuple[int, int]:
    media_pk, creator_id = seed_candidate(conn, code=code, username=username)
    conn.execute(
        "UPDATE candidate_media SET target_score = ?, caption = ?, hashtags = ? WHERE media_pk = ?",
        (score, caption, json.dumps(list(hashtags), ensure_ascii=False), media_pk),
    )
    conn.execute(
        "INSERT INTO media_analysis (media_pk, analyzer, analyzer_version, prompt_version, "
        "payload, analyzed_at) VALUES (?, 'claude_code', '1', '1', ?, datetime('now'))",
        (media_pk, json.dumps({"topics": list(topics), "keywords": []}, ensure_ascii=False)),
    )
    conn.commit()
    return media_pk, creator_id


def add_draft(
    conn: sqlite3.Connection, media_pk: int, text: str, *, quality_ok: bool = True
) -> int:
    draft_id = save_comment_draft(
        conn,
        media_pk,
        text,
        text.strip().lower(),
        language="ko",
        quality_ok=quality_ok,
        quality_reason="" if quality_ok else "generic",
        similarity_max=0.1,
        status="PENDING",
        generator="template",
        generator_version="1",
    )
    conn.commit()
    return int(draft_id)


# --- 19C Query Expansion ---------------------------------------------------
def test_조합은_결정론적이다():
    first = combine(["퇴근", "러닝", "한강"], limit=5)
    second = combine(["퇴근", "러닝", "한강"], limit=5)

    assert first == second
    assert "퇴근 러닝" in first


def test_고득점_후보가_부족하면_확장하지_않는다(config: Config, conn: sqlite3.Connection):
    seed_bank(conn)
    scored_candidate(conn)

    plan = expand_queries(conn, config, now=NOW, force=True)

    assert plan.status == STATUS_NOT_ENOUGH_DATA
    assert plan.added == []


def test_고득점_후보에서_새_검색어를_만든다(config: Config, conn: sqlite3.Connection):
    seed_bank(conn)
    for index in range(10):
        scored_candidate(conn, code=f"S{index:04d}", username=f"creator_{index}")

    plan = expand_queries(conn, config, now=NOW, force=True)

    assert plan.status == STATUS_OK
    assert plan.added
    assert len(plan.added) <= int(config.get("query_planner.expansion.max_new_queries"))
    assert plan.sources >= 10


def test_새_검색어는_experimental로_들어간다(config: Config, conn: sqlite3.Connection):
    seed_bank(conn)
    for index in range(10):
        scored_candidate(conn, code=f"S{index:04d}", username=f"creator_{index}")

    plan = expand_queries(conn, config, now=NOW, force=True)
    record = get_query(conn, plan.added[0])

    assert record.status is QueryStatus.EXPERIMENTAL  # 바로 ACTIVE가 아니다
    assert record.origin == ORIGIN_EXPANSION


def test_이미_있는_검색어는_추가하지_않는다(config: Config, conn: sqlite3.Connection):
    seed_bank(conn)
    for index in range(10):
        scored_candidate(conn, code=f"S{index:04d}", username=f"creator_{index}")

    first = expand_queries(conn, config, now=NOW, force=True)
    second = expand_queries(conn, config, now=NOW, force=True)

    assert set(first.added).isdisjoint(second.added)
    assert all(reason == "already_in_bank" for reason in second.rejected.values())


def test_주기가_되지_않으면_건너뛴다(config: Config, conn: sqlite3.Connection):
    seed_bank(conn)
    for index in range(10):
        scored_candidate(conn, code=f"S{index:04d}", username=f"creator_{index}")
    expand_queries(conn, config, now=NOW, force=True)

    again = expand_queries(conn, config, now=NOW + timedelta(hours=1))

    assert again.status == STATUS_SKIPPED
    assert again.added == []


def test_확장_현황을_볼_수_있다(config: Config, conn: sqlite3.Connection):
    seed_bank(conn)
    for index in range(10):
        scored_candidate(conn, code=f"S{index:04d}", username=f"creator_{index}")
    expand_queries(conn, config, now=NOW, force=True)

    summary = expansion_summary(conn)

    assert summary["total"] >= 1
    assert summary["by_status"].get("EXPERIMENTAL", 0) >= 1
    assert summary["last_run_at"] is not None


def test_agent의_action은_확장_근거가_아니다(config: Config, conn: sqlite3.Connection):
    """LIKE/COMMENT 기록만으로는 확장 근거가 되지 않는다(자기 강화 방지)."""
    seed_bank(conn)
    media_pk, creator_id = seed_candidate(conn)  # 점수 없음
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

    plan = expand_queries(conn, config, now=NOW, force=True)

    assert plan.status == STATUS_NOT_ENOUGH_DATA
    assert plan.sources == 0


# --- 19F Comment Eligibility ----------------------------------------------
def test_점수가_낮으면_comment_불가(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn, score=76.0)
    add_draft(conn, media_pk, "저도 퇴근하고 뛰고 싶네요")

    result = comment_eligibility(conn, config, media_pk, 76.0)

    assert result.ok is False
    assert result.reason == COMMENT_BELOW_THRESHOLD


def test_caption이_없으면_comment_불가(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn, caption="")
    add_draft(conn, media_pk, "저도 퇴근하고 뛰고 싶네요")

    result = comment_eligibility(conn, config, media_pk, 90.0)

    assert result.ok is False
    assert result.reason == COMMENT_NO_CAPTION


def test_품질_통과_댓글이_있으면_comment_가능(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn)
    draft_id = add_draft(conn, media_pk, "저도 퇴근하고 한강 뛰러 가고 싶네요")

    result = comment_eligibility(conn, config, media_pk, 90.0)

    assert result.ok is True
    assert result.draft_id == draft_id
    assert "한강" in result.text


def test_품질_탈락만_있으면_comment_불가(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn)
    add_draft(conn, media_pk, "잘 보고 갑니다", quality_ok=False)

    result = comment_eligibility(conn, config, media_pk, 90.0)

    assert result.ok is False
    assert result.reason == COMMENT_QUALITY_FAIL


def test_같은_creator에게_최근_댓글을_달았으면_제외한다(config: Config, conn: sqlite3.Connection):
    media_pk, creator_id = scored_candidate(conn)
    add_draft(conn, media_pk, "저도 퇴근하고 뛰고 싶네요")
    record_interaction(
        conn,
        media_pk=media_pk,
        creator_id=creator_id,
        action_id=None,
        action_type=ActionType.COMMENT,
        executor="browser",
        dry_run=False,
        success=True,
    )
    conn.commit()

    result = comment_eligibility(conn, config, media_pk, 90.0)

    assert result.ok is False
    assert result.reason == COMMENT_RECENT_SAME_CREATOR


def test_comment를_끄면_불가(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn)
    add_draft(conn, media_pk, "저도 퇴근하고 뛰고 싶네요")
    raw = {**config.raw, "actions": {**config.raw["actions"], "enable_comment": False}}
    off = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    assert comment_eligibility(conn, off, media_pk, 90.0).ok is False


# --- 19I Action Mix --------------------------------------------------------
def test_기준_미달이면_action이_없다(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn, score=60.0)

    mix = decide_action_mix(conn, config, media_pk, 60.0)

    assert mix.actions == ()
    assert mix.reason == "below_threshold"


def test_like_기준만_넘으면_like만(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn, score=78.0)
    add_draft(conn, media_pk, "저도 퇴근하고 뛰고 싶네요")

    mix = decide_action_mix(conn, config, media_pk, 78.0)

    assert mix.actions == (ActionType.LIKE,)
    assert mix.has_comment is False


def test_comment_기준_이상이고_품질_통과면_like와_comment(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn, score=90.0)
    add_draft(conn, media_pk, "저도 퇴근하고 한강 뛰러 가고 싶네요")

    mix = decide_action_mix(conn, config, media_pk, 90.0)

    assert mix.actions == (ActionType.LIKE, ActionType.COMMENT)
    assert mix.comment.quality_pass is True


def test_comment_품질_실패면_like만_남긴다(config: Config, conn: sqlite3.Connection):
    """후보 전체를 버리지 않는다 — 댓글만 빼고 LIKE는 진행한다."""
    media_pk, _ = scored_candidate(conn, score=90.0)
    add_draft(conn, media_pk, "잘 보고 갑니다", quality_ok=False)

    mix = decide_action_mix(conn, config, media_pk, 90.0)

    assert mix.actions == (ActionType.LIKE,)
    assert mix.reason.startswith("like_only:")
    assert mix.comment.reason == COMMENT_QUALITY_FAIL


def test_임계값은_config에서만_읽는다(config: Config, conn: sqlite3.Connection):
    media_pk, _ = scored_candidate(conn, score=78.0)
    add_draft(conn, media_pk, "저도 퇴근하고 한강 뛰러 가고 싶네요")
    raw = {
        **config.raw,
        "actions": {**config.raw["actions"], "require_score_for_comment": 70},
    }
    lower = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    mix = decide_action_mix(conn, lower, media_pk, 78.0)

    assert mix.actions == (ActionType.LIKE, ActionType.COMMENT)
    assert ActionPolicy.from_config(lower).comment_threshold == 70


# --- 19H Activity Controller ----------------------------------------------
def test_기본_상태는_normal이고_설정_상한을_쓴다(config: Config, conn: sqlite3.Connection):
    status = activity_status(conn, config, now=NOW)

    assert status.state is ActivityState.NORMAL
    assert status.can_act is True
    assert status.limit_for(ActionType.LIKE) == int(config.get("actions.daily_limits.likes"))


def test_경고를_보면_caution으로_내려간다(config: Config, conn: sqlite3.Connection):
    observe(conn, "PLATFORM_WARNING", now=NOW)

    status = activity_status(conn, config, now=NOW)

    assert status.state is ActivityState.CAUTION
    assert status.can_act is True
    assert status.limit_for(ActionType.LIKE) < status.configured_limits["LIKE"]


@pytest.mark.parametrize("signal", ["ACTION_BLOCKED", "CHALLENGE", "LOGIN_REQUIRED"])
def test_차단_신호는_stopped다(config: Config, conn: sqlite3.Connection, signal: str):
    observe(conn, signal, now=NOW)

    status = activity_status(conn, config, now=NOW)

    assert status.state is ActivityState.STOPPED
    assert status.can_act is False
    assert status.limit_for(ActionType.LIKE) == 0


def test_좋아지는_방향으로는_자동_변경하지_않는다(config: Config, conn: sqlite3.Connection):
    """경고가 없다고 활동량을 자동으로 늘리지 않는다."""
    observe(conn, "ACTION_BLOCKED", now=NOW)

    observe(conn, "PLATFORM_WARNING", now=NOW)  # 더 약한 신호
    observe(conn, None, now=NOW)

    assert activity_status(conn, config, now=NOW).state is ActivityState.STOPPED


def test_stopped는_시간이_지나도_자동으로_풀리지_않는다(config: Config, conn: sqlite3.Connection):
    observe(conn, "ACTION_BLOCKED", now=NOW)

    later = activity_status(conn, config, now=NOW + timedelta(days=30))

    assert later.state is ActivityState.STOPPED  # 사람이 확인해야 한다


def test_caution은_조용한_기간이_지나면_복귀한다(config: Config, conn: sqlite3.Connection):
    observe(conn, "PLATFORM_WARNING", now=NOW)

    later = activity_status(conn, config, now=NOW + timedelta(hours=25))

    assert later.state is ActivityState.NORMAL


def test_운영자는_상태를_되돌릴_수_있다(config: Config, conn: sqlite3.Connection):
    observe(conn, "ACTION_BLOCKED", now=NOW)

    reset(conn)

    assert activity_status(conn, config, now=NOW).state is ActivityState.NORMAL


def test_caution_상한은_설정값을_넘지_못한다(config: Config, conn: sqlite3.Connection):
    """ratio를 1보다 크게 줘도 설정 상한을 넘기지 않는다(자동 증가 금지)."""
    observe(conn, "PLATFORM_WARNING", now=NOW)
    raw = {**config.raw, "activity": {"caution_ratio": 5.0, "caution_recovery_hours": 24}}
    weird = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    status = activity_status(conn, weird, now=NOW)

    assert status.limit_for(ActionType.LIKE) <= status.configured_limits["LIKE"]


def test_effective_config는_일일_상한만_낮춘다(config: Config, conn: sqlite3.Connection):
    observe(conn, "PLATFORM_WARNING", now=NOW)
    status = activity_status(conn, config, now=NOW)

    reduced = effective_config(config, status)

    assert int(reduced.get("actions.daily_limits.likes")) == status.limit_for(ActionType.LIKE)
    # 다른 설정은 그대로다
    assert reduced.get("actions.require_score_for_comment") == config.get(
        "actions.require_score_for_comment"
    )
