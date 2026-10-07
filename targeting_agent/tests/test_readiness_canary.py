"""Canary 추적 테스트 (Phase 18D.6).

후보 → 분석 → 댓글 → Action → 실행이 **서로 연결돼 있는지** 본다.
각 단계가 존재하는 것만으로는 "왜 이게 나갔는지"를 설명할 수 없다.
"""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.core.database import APPROVED_BY_OPERATOR, approve_action
from targeting_agent.readiness.audit import collect_chain

RUN_ID = "run-canary"


def _seed(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    score: float | None = 90.0,
    with_analysis: bool = True,
    with_draft: bool = True,
    permalink: str | None = None,
    canonical_url: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO creators (username, status, first_seen_at, updated_at) "
        "VALUES (?, 'NEW', '2026-10-01', '2026-10-01')",
        (f"user_{media_id}",),
    )
    creator_id = int(cursor.lastrowid)
    default_url = f"https://www.instagram.com/reel/{media_id}/"
    cursor = conn.execute(
        """INSERT INTO candidate_media
           (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
            source, status, target_score, discovered_at, updated_at)
           VALUES (?, ?, ?, ?, 'REEL', '퇴근길 기록', '[]', 'instagram_browser_search',
                   'SCORED', ?, '2026-10-01', '2026-10-01')""",
        (
            media_id,
            creator_id,
            default_url if permalink is None else permalink,
            default_url if canonical_url is None else canonical_url,
            score,
        ),
    )
    media_pk = int(cursor.lastrowid)
    if with_analysis:
        conn.execute(
            """INSERT INTO media_analysis
               (media_pk, analyzer, analyzer_version, prompt_version, payload, similarity,
                target_score, score_breakdown, analyzed_at)
               VALUES (?, 'claude_code', '1', '1', '{}', 0.9, ?, '{}', '2026-10-01')""",
            (media_pk, score),
        )
    if with_draft:
        conn.execute(
            """INSERT INTO comment_drafts
               (media_pk, text, normalized_text, language, length, quality_ok, quality_reason,
                similarity_max, status, generator, generator_version, created_at)
               VALUES (?, '퇴근길 풍경 좋네요', '퇴근길 풍경 좋네요', 'ko', 10, 1, '', 0.0,
                       'SELECTED', 'claude_code', '1', '2026-10-01')""",
            (media_pk,),
        )
    return media_pk


def _queue(
    conn: sqlite3.Connection,
    media_pk: int,
    *,
    action_type: str = "LIKE",
    status: str = "PENDING",
    comment_text: str | None = None,
    run_id: str = RUN_ID,
) -> int:
    creator_id = int(
        conn.execute(
            "SELECT creator_id FROM candidate_media WHERE media_pk = ?", (media_pk,)
        ).fetchone()[0]
    )
    cursor = conn.execute(
        """INSERT INTO action_queue
           (run_id, media_pk, creator_id, action_type, comment_text, target_score, priority,
            status, executor, dry_run, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 90.0, 90, ?, 'browser', 1, '2026-10-01', '2026-10-01')""",
        (run_id, media_pk, creator_id, action_type, comment_text, status),
    )
    return int(cursor.lastrowid)


def _execute(conn: sqlite3.Connection, action_id: int, media_pk: int, action_type: str) -> None:
    creator_id = int(
        conn.execute(
            "SELECT creator_id FROM candidate_media WHERE media_pk = ?", (media_pk,)
        ).fetchone()[0]
    )
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_id, action_type, executor, dry_run, success, executed_at)
           VALUES (?, ?, ?, ?, 'browser', 1, 1, '2026-10-01T00:00:00+00:00')""",
        (media_pk, creator_id, action_id, action_type),
    )
    conn.execute(
        "UPDATE action_queue SET status = 'SUCCESS', finished_at = '2026-10-01T00:00:01+00:00' "
        "WHERE action_id = ?",
        (action_id,),
    )


def test_완전한_사슬은_끊긴_곳이_없다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111")
    action_id = _queue(conn, media_pk, action_type="COMMENT", comment_text="퇴근길 풍경 좋네요")
    approve_action(conn, action_id, by=APPROVED_BY_OPERATOR)
    _execute(conn, action_id, media_pk, "COMMENT")

    links = collect_chain(conn, RUN_ID)

    assert len(links) == 1
    assert links[0].complete, links[0].broken
    assert links[0].approved_by == APPROVED_BY_OPERATOR
    assert links[0].executed is True


def test_분석이_없으면_끊긴_것으로_본다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111", with_analysis=False)
    _queue(conn, media_pk)

    links = collect_chain(conn, RUN_ID)

    assert "analysis_missing" in links[0].broken


def test_점수가_없으면_끊긴_것으로_본다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111", score=None, with_analysis=False)
    _queue(conn, media_pk)

    links = collect_chain(conn, RUN_ID)

    assert "score_missing" in links[0].broken


def test_댓글_action인데_초안이_없으면_끊긴_것이다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111", with_draft=False)
    _queue(conn, media_pk, action_type="COMMENT", comment_text="x")

    links = collect_chain(conn, RUN_ID)

    assert "comment_draft_missing" in links[0].broken


def test_실행된_댓글_action에_본문이_없으면_끊긴_것이다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111")
    action_id = _queue(conn, media_pk, action_type="COMMENT", comment_text=None)
    _execute(conn, action_id, media_pk, "COMMENT")

    links = collect_chain(conn, RUN_ID)

    assert "comment_text_missing" in links[0].broken


def test_상태는_성공인데_실행기록이_없으면_끊긴_것이다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111")
    action_id = _queue(conn, media_pk)
    conn.execute(
        "UPDATE action_queue SET status = 'SUCCESS' WHERE action_id = ?", (action_id,)
    )

    links = collect_chain(conn, RUN_ID)

    assert "interaction_missing" in links[0].broken


def test_canonical_url이_없어도_permalink가_있으면_끊기지_않는다(conn: sqlite3.Connection):
    """Executor는 canonical_url이 없으면 permalink로 연다.

    검사가 실제 동작보다 엄하면 멀쩡한 Action을 끊긴 것으로 보고하게 된다.
    """
    media_pk = _seed(conn, "AAA111", canonical_url="")
    _queue(conn, media_pk)

    links = collect_chain(conn, RUN_ID)

    assert links[0].complete, links[0].broken
    assert links[0].canonical_url.endswith("/reel/AAA111/")


def test_열_주소가_아예_없으면_끊긴_것이다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111", canonical_url="", permalink="")
    _queue(conn, media_pk)

    links = collect_chain(conn, RUN_ID)

    assert "target_url_missing" in links[0].broken


def test_run_id를_주면_그_run이_만든_action만_본다(conn: sqlite3.Connection):
    media_pk = _seed(conn, "AAA111")
    _queue(conn, media_pk, run_id="run-old")
    _queue(conn, _seed(conn, "BBB222"), action_type="COMMENT", run_id=RUN_ID,
           comment_text="퇴근길 풍경 좋네요")

    assert len(collect_chain(conn, RUN_ID)) == 1
    assert len(collect_chain(conn, "run-old")) == 1


def test_run_id를_주지_않으면_실행된_action을_본다(conn: sqlite3.Connection):
    """한 Run이 실행하는 Action은 **앞선 Run에서 만들어졌을 수** 있다.

    이걸 구분하지 않으면 '실행은 됐는데 추적이 안 되는' 것처럼 보인다.
    """
    old_media = _seed(conn, "AAA111")
    old_action = _queue(conn, old_media, run_id="run-old")
    approve_action(conn, old_action, by=APPROVED_BY_OPERATOR)
    _execute(conn, old_action, old_media, "LIKE")
    _queue(conn, _seed(conn, "BBB222"), run_id=RUN_ID)  # 새 Run이 만들었지만 미실행

    executed = collect_chain(conn)

    assert [link.action_id for link in executed] == [old_action]
    assert executed[0].complete


@pytest.mark.parametrize("run_id", [RUN_ID, None])
def test_추적은_db를_바꾸지_않는다(conn: sqlite3.Connection, run_id):
    media_pk = _seed(conn, "AAA111")
    _queue(conn, media_pk)
    before = conn.execute("SELECT COUNT(*) FROM action_queue").fetchone()[0]

    collect_chain(conn, run_id)

    assert conn.execute("SELECT COUNT(*) FROM action_queue").fetchone()[0] == before
