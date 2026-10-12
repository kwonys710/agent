"""Action Policy Projection 테스트 (Phase 18D.4).

추정만 한다 — Action을 만들지도, 실행하지도, 저장하지도 않는다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.core.config import Config
from targeting_agent.readiness.projection import (
    BLOCK_ALREADY,
    BLOCK_BELOW_MINIMUM,
    BLOCK_COOLDOWN,
    BLOCK_CREATOR_DAILY,
    BLOCK_DAILY_LIMIT,
    BLOCK_NO_COMMENT,
    BLOCK_UNRESOLVED,
    BROWSER_SOURCE,
    collect_projection,
)

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def _creator(
    conn: sqlite3.Connection,
    username: str,
    *,
    last_interacted_at: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO creators (username, status, last_interacted_at, first_seen_at, updated_at) "
        "VALUES (?, 'NEW', ?, '2026-10-01', '2026-10-01')",
        (username, last_interacted_at),
    )
    return int(cursor.lastrowid)


def _candidate(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    score: float,
    creator_id: int,
    source: str = BROWSER_SOURCE,
    with_comment: bool = True,
) -> int:
    cursor = conn.execute(
        """INSERT INTO candidate_media
           (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
            source, status, target_score, discovered_at, updated_at)
           VALUES (?, ?, ?, ?, 'REEL', '', '[]', ?, 'SCORED', ?, '2026-10-01', '2026-10-01')""",
        (
            media_id,
            creator_id,
            f"https://www.instagram.com/reel/{media_id}/",
            f"https://www.instagram.com/reel/{media_id}/",
            source,
            score,
        ),
    )
    media_pk = int(cursor.lastrowid)
    if with_comment:
        conn.execute(
            """INSERT INTO comment_drafts
               (media_pk, text, normalized_text, language, length, quality_ok, quality_reason,
                similarity_max, status, generator, generator_version, created_at)
               VALUES (?, '퇴근길 풍경 좋네요', '퇴근길 풍경 좋네요', 'ko', 10, 1, '', 0.0,
                       'CANDIDATE', 'claude_code', '1', '2026-10-01')""",
            (media_pk,),
        )
    return media_pk


def _project(conn: sqlite3.Connection, config: Config):
    return collect_projection(conn, config, now=NOW).current


# --- 임계값 ---------------------------------------------------------------
def test_점수가_하한_미만이면_자격이_없다(conn: sqlite3.Connection, config: Config):
    creator_id = _creator(conn, "a")
    _candidate(conn, "AAA111", score=50.0, creator_id=creator_id)

    projection = _project(conn, config)

    assert projection.like_eligible == 0
    assert projection.blocked[BLOCK_BELOW_MINIMUM] == 1


def test_like_임계값만_넘으면_like만_나간다(conn: sqlite3.Connection, config: Config):
    creator_id = _creator(conn, "a")
    _candidate(conn, "AAA111", score=78.0, creator_id=creator_id)

    projection = _project(conn, config)

    assert (projection.like_eligible, projection.comment_eligible) == (1, 0)
    assert (projection.projected_likes, projection.projected_comments) == (1, 0)


def test_comment_임계값을_넘으면_둘_다_나간다(conn: sqlite3.Connection, config: Config):
    creator_id = _creator(conn, "a")
    _candidate(conn, "AAA111", score=90.0, creator_id=creator_id)

    projection = _project(conn, config)

    assert (projection.like_eligible, projection.comment_eligible) == (1, 1)
    assert (projection.projected_likes, projection.projected_comments) == (1, 1)


# --- 가드 -----------------------------------------------------------------
def test_하루_한도를_넘기지_않는다(conn: sqlite3.Connection, config: Config):
    """후보가 아무리 많아도 하루 한도 이상은 나가지 않는다."""
    raw = {
        **config.raw,
        "actions": {
            **config.raw["actions"],
            "daily_limits": {**config.raw["actions"]["daily_limits"], "likes": 2, "comments": 1},
        },
    }
    limited = Config(raw=raw, path=config.path, base_dir=config.base_dir)
    for index in range(6):
        _candidate(
            conn, f"CODE{index}", score=90.0, creator_id=_creator(conn, f"user{index}")
        )

    projection = collect_projection(conn, limited, now=NOW).current

    assert projection.projected_likes == 2
    assert projection.projected_comments == 1
    assert projection.blocked[BLOCK_DAILY_LIMIT] > 0


def test_같은_creator는_하루_한도를_넘지_않는다(conn: sqlite3.Connection, config: Config):
    """후보가 여러 개여도 같은 작성자에게 반복 접촉하지 않는다."""
    creator_id = _creator(conn, "same_person")
    for index in range(4):
        _candidate(conn, f"CODE{index}", score=90.0, creator_id=creator_id)

    projection = _project(conn, config)

    assert projection.projected_likes == 1  # per_creator.max_actions_per_day = 1
    assert projection.blocked[BLOCK_CREATOR_DAILY] == 3


def test_cooldown_중인_creator는_제외된다(conn: sqlite3.Connection, config: Config):
    recent = (NOW - timedelta(days=2)).isoformat()
    creator_id = _creator(conn, "recent", last_interacted_at=recent)
    _candidate(conn, "AAA111", score=90.0, creator_id=creator_id)

    projection = _project(conn, config)

    assert projection.projected_total == 0
    assert projection.blocked[BLOCK_COOLDOWN] == 1


def test_cooldown이_지난_creator는_다시_대상이다(conn: sqlite3.Connection, config: Config):
    old = (NOW - timedelta(days=30)).isoformat()
    creator_id = _creator(conn, "old", last_interacted_at=old)
    _candidate(conn, "AAA111", score=90.0, creator_id=creator_id)

    projection = _project(conn, config)

    assert projection.projected_likes == 1
    assert BLOCK_COOLDOWN not in projection.blocked


def test_이미_접촉한_게시물은_제외된다(conn: sqlite3.Connection, config: Config):
    creator_id = _creator(conn, "a")
    media_pk = _candidate(conn, "AAA111", score=90.0, creator_id=creator_id)
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_type, executor, dry_run, success, executed_at)
           VALUES (?, ?, 'LIKE', 'browser', 1, 1, '2026-10-01T00:00:00+00:00')""",
        (media_pk, creator_id),
    )

    projection = _project(conn, config)

    assert projection.projected_total == 0
    assert projection.blocked[BLOCK_ALREADY] == 1


def test_creator_미확인_후보는_제외된다(conn: sqlite3.Connection, config: Config):
    creator_id = _creator(conn, "unresolved:AAA111")
    _candidate(conn, "AAA111", score=90.0, creator_id=creator_id)

    projection = _project(conn, config)

    assert projection.projected_total == 0
    assert projection.blocked[BLOCK_UNRESOLVED] == 1


def test_댓글_후보가_없으면_comment는_나가지_않는다(conn: sqlite3.Connection, config: Config):
    """COMMENT는 내용 없이 실행될 수 없다."""
    creator_id = _creator(conn, "a")
    _candidate(conn, "AAA111", score=90.0, creator_id=creator_id, with_comment=False)

    projection = _project(conn, config)

    assert projection.projected_likes == 1
    assert projection.projected_comments == 0
    assert projection.blocked[BLOCK_NO_COMMENT] == 1


# --- 시나리오 / 출처 --------------------------------------------------------
def test_시나리오는_운영_설정을_바꾸지_않는다(conn: sqlite3.Connection, config: Config):
    _candidate(conn, "AAA111", score=78.0, creator_id=_creator(conn, "a"))
    before = (
        config.get("actions.require_score_for_like"),
        config.get("actions.require_score_for_comment"),
    )

    report = collect_projection(conn, config, scenarios=((60.0, 65.0),), now=NOW)

    after = (
        config.get("actions.require_score_for_like"),
        config.get("actions.require_score_for_comment"),
    )
    assert before == after
    assert report.current.like_threshold == float(before[0])
    assert report.scenarios[0].like_threshold == 60.0


def test_자격_후보의_출처를_구분한다(conn: sqlite3.Connection, config: Config):
    _candidate(conn, "AAA111", score=90.0, creator_id=_creator(conn, "a"))
    _candidate(
        conn, "BBB222", score=90.0, creator_id=_creator(conn, "b"), source="import:sample.csv"
    )

    projection = _project(conn, config)

    assert projection.eligible_by_source == {BROWSER_SOURCE: 1, "import:sample.csv": 1}


def test_browser_후보가_하나도_자격이_없으면_알려준다(conn: sqlite3.Connection, config: Config):
    """수집은 되는데 Action으로 이어지지 않는 상태를 그냥 넘기지 않는다."""
    for index in range(3):
        _candidate(conn, f"CODE{index}", score=68.0, creator_id=_creator(conn, f"u{index}"))

    report = collect_projection(conn, config, now=NOW)

    assert report.current.eligible_by_source == {}
    assert any("Action 자격을 얻은 건이" in note for note in report.notes)


def test_사람_신호가_없으면_어느_시나리오가_낫다고_말하지_않는다(
    conn: sqlite3.Connection, config: Config
):
    _candidate(conn, "AAA111", score=90.0, creator_id=_creator(conn, "a"))

    report = collect_projection(conn, config, now=NOW)

    assert report.ground_truth_available is False
    assert any("분포만 보여" in note for note in report.notes)


def test_사람_신호가_있으면_겹침을_센다(conn: sqlite3.Connection, config: Config):
    creator_id = _creator(conn, "a")
    media_pk = _candidate(conn, "AAA111", score=90.0, creator_id=creator_id)
    conn.execute(
        "INSERT INTO feedback (media_pk, creator_id, feedback_type, value, created_at) "
        "VALUES (?, ?, 'GOOD_TARGET', 1.0, '2026-10-01')",
        (media_pk, creator_id),
    )

    report = collect_projection(conn, config, now=NOW)

    assert report.ground_truth_available is True
    assert report.current.good_target_overlap == 1


def test_추정은_db를_바꾸지_않는다(conn: sqlite3.Connection, config: Config):
    _candidate(conn, "AAA111", score=90.0, creator_id=_creator(conn, "a"))
    before = (
        conn.execute("SELECT COUNT(*) FROM action_queue").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0],
    )

    collect_projection(conn, config, now=NOW)

    after = (
        conn.execute("SELECT COUNT(*) FROM action_queue").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0],
    )
    assert before == after == (0, 0)


@pytest.mark.parametrize("score", [69.9, 70.0, 74.9, 75.0, 81.9, 82.0])
def test_임계값_경계는_운영_정책과_같다(conn: sqlite3.Connection, config: Config, score: float):
    """추정이 자체 규칙을 쓰면 실제와 다른 숫자가 나온다 — ActionPolicy를 그대로 쓴다."""
    from targeting_agent.actions.policy import ActionPolicy
    from targeting_agent.core.models import ActionType

    _candidate(conn, "AAA111", score=score, creator_id=_creator(conn, "a"))
    policy = ActionPolicy.from_config(config)
    expected = policy.recommend(score)

    projection = _project(conn, config)

    assert projection.like_eligible == (1 if ActionType.LIKE in expected else 0)
    assert projection.comment_eligible == (1 if ActionType.COMMENT in expected else 0)
