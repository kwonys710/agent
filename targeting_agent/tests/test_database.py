"""SQLite 스키마/중복 방지 테스트."""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.core.database import (
    enqueue_action,
    fetch_candidates,
    has_interaction,
    insert_candidate,
    record_interaction,
    update_candidate_status,
)
from targeting_agent.core.models import ActionType, MediaStatus

EXPECTED_TABLES = {
    "creators", "candidate_media", "media_analysis", "comment_drafts",
    "action_queue", "interactions", "feedback", "daily_stats", "app_state",
}


def test_schema_tables(conn) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert EXPECTED_TABLES <= {row["name"] for row in rows}


def test_duplicate_media_blocked(conn, sample_candidate) -> None:
    assert insert_candidate(conn, sample_candidate) is not None
    assert insert_candidate(conn, sample_candidate) is None  # 동일 media_id 재수집 차단
    assert conn.execute("SELECT COUNT(*) FROM candidate_media").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0] == 1


def test_duplicate_action_blocked(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    kwargs = dict(
        run_id="r1", media_pk=media_pk, creator_id=1, action_type=ActionType.LIKE,
        target_score=90.0, priority=90, executor="manual", dry_run=True,
    )
    assert enqueue_action(conn, **kwargs) is not None
    assert enqueue_action(conn, **kwargs) is None


def test_interaction_uniqueness_and_lookup(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    kwargs = dict(
        media_pk=media_pk, creator_id=1, action_id=None, executor="manual",
        dry_run=True, success=True,
    )
    assert record_interaction(conn, action_type=ActionType.LIKE, **kwargs) is not None
    assert record_interaction(conn, action_type=ActionType.LIKE, **kwargs) is None
    # 같은 게시물이라도 종류가 다르면 기록된다(LIKE 후 COMMENT).
    assert record_interaction(
        conn, action_type=ActionType.COMMENT, comment_text="테스트", **kwargs
    ) is not None

    assert has_interaction(conn, media_pk, True, ActionType.LIKE) is True
    assert has_interaction(conn, media_pk + 99, True) is False


def test_foreign_key_enforced(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidate_media (media_id, creator_id, permalink, discovered_at, updated_at) "
            "VALUES ('X', 9999, 'u', 'now', 'now')"
        )


def test_status_transition_and_fetch(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    assert len(fetch_candidates(conn, [MediaStatus.NEW.value])) == 1
    update_candidate_status(conn, media_pk, MediaStatus.SCORED, "scored", 88.5)
    rows = fetch_candidates(conn, [MediaStatus.SCORED.value])
    assert rows[0]["target_score"] == 88.5
    assert not fetch_candidates(conn, [MediaStatus.NEW.value])
