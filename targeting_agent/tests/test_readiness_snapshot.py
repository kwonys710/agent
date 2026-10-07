"""Operational Baseline Snapshot / Audit Trail 테스트 (Phase 18D.0 · 18D.1).

실제 Instagram·Claude를 쓰지 않는다. 메모리 DB에 값을 직접 넣고 집계만 확인한다.
"""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.core.database import (
    APPROVED_BY_AUTOPILOT,
    APPROVED_BY_OPERATOR,
    approve_action,
)
from targeting_agent.readiness.audit import (
    CREATED_BY_AUTOPILOT,
    CREATED_BY_PIPELINE,
    NOT_EXECUTED,
    UNKNOWN,
    collect_audit,
)
from targeting_agent.readiness.snapshot import collect_baseline, pct


# --- 작은 도우미: 테스트가 읽는 최소 행만 넣는다 ---------------------------
def _creator(conn: sqlite3.Connection, username: str) -> int:
    cursor = conn.execute(
        "INSERT INTO creators (username, status, first_seen_at, updated_at) "
        "VALUES (?, 'NEW', '2026-10-01', '2026-10-01')",
        (username,),
    )
    return int(cursor.lastrowid)


def _media(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    creator_id: int,
    source: str = "instagram_browser_search",
    status: str = "NEW",
) -> int:
    cursor = conn.execute(
        """INSERT INTO candidate_media
           (media_id, creator_id, permalink, canonical_url, media_type, caption,
            hashtags, source, status, discovered_at, updated_at)
           VALUES (?, ?, ?, ?, 'REEL', '', '[]', ?, ?, '2026-10-01', '2026-10-01')""",
        (
            media_id,
            creator_id,
            f"https://www.instagram.com/reel/{media_id}/",
            f"https://www.instagram.com/reel/{media_id}/",
            source,
            status,
        ),
    )
    return int(cursor.lastrowid)


def _action(
    conn: sqlite3.Connection,
    *,
    media_pk: int,
    creator_id: int,
    run_id: str,
    action_type: str = "LIKE",
    executor: str = "manual",
    status: str = "PENDING",
) -> int:
    cursor = conn.execute(
        """INSERT INTO action_queue
           (run_id, media_pk, creator_id, action_type, target_score, priority, status,
            executor, dry_run, created_at, updated_at)
           VALUES (?, ?, ?, ?, 80.0, 1, ?, ?, 1, '2026-10-01', '2026-10-01')""",
        (run_id, media_pk, creator_id, action_type, status, executor),
    )
    return int(cursor.lastrowid)


def _interaction(
    conn: sqlite3.Connection,
    *,
    action_id: int,
    media_pk: int,
    creator_id: int,
    executor: str,
    dry_run: bool,
    success: bool = True,
    action_type: str = "LIKE",
) -> None:
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_id, action_type, executor, dry_run, success, executed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-01T00:00:00+00:00')""",
        (media_pk, creator_id, action_id, action_type, executor, int(dry_run), int(success)),
    )


def _finish(conn: sqlite3.Connection, action_id: int, status: str, error: str = "") -> None:
    conn.execute(
        "UPDATE action_queue SET status = ?, started_at = ?, finished_at = ?, error = ? "
        "WHERE action_id = ?",
        (status, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:01+00:00", error, action_id),
    )


# ===========================================================================
# 18D.0 — Baseline Snapshot
# ===========================================================================
def test_표본이_없으면_비율은_0이_아니라_미측정이다():
    """0건을 0%로 쓰면 '완벽하다'로 오독된다 — 측정 못 한 것은 '-'로 둔다."""
    assert pct(None) == "-"
    assert pct(0.0) == "0.0%"
    assert pct(1.0) == "100.0%"


def test_기준선은_후보와_댓글과_action을_센다(conn: sqlite3.Connection):
    creator_id = _creator(conn, "office_daily_kim")
    media_pk = _media(conn, "AAA111", creator_id=creator_id)
    _media(conn, "BBB222", creator_id=creator_id, source="dashboard_manual", status="NEEDS_ENRICHMENT")
    conn.execute(
        """INSERT INTO comment_drafts
           (media_pk, text, normalized_text, language, length, quality_ok, quality_reason,
            similarity_max, status, generator, generator_version, created_at)
           VALUES (?, '좋네요', '좋네요', 'ko', 3, 0, 'generic:잘 보고', 0.0,
                   'REJECTED', 'claude_code', '1', '2026-10-01')""",
        (media_pk,),
    )
    _action(conn, media_pk=media_pk, creator_id=creator_id, run_id="run-1")

    snapshot = collect_baseline(conn)

    assert snapshot.candidates.total == 2
    assert snapshot.candidates.browser_total == 1
    assert snapshot.candidates.needs_enrichment == 1
    assert snapshot.comments.total == 1 and snapshot.comments.rejected == 1
    assert snapshot.actions.queued == 1
    assert snapshot.actions.real_writes == 0


def test_거절_사유는_변수부분을_떼고_묶는다(conn: sqlite3.Connection):
    """'duplicate(0.89)'와 'duplicate(0.91)'은 같은 사유 한 종류다."""
    creator_id = _creator(conn, "a")
    media_pk = _media(conn, "AAA111", creator_id=creator_id)
    for reason in ("duplicate(0.89)", "duplicate(0.91)", "generic:잘 보고", "no_context_keyword"):
        conn.execute(
            """INSERT INTO comment_drafts
               (media_pk, text, normalized_text, language, length, quality_ok, quality_reason,
                similarity_max, status, generator, generator_version, created_at)
               VALUES (?, ?, ?, 'ko', 3, 0, ?, 0.0, 'REJECTED', 'claude_code', '1', '2026-10-01')""",
            (media_pk, reason, reason, reason),
        )

    snapshot = collect_baseline(conn)

    assert snapshot.comments.reject_reasons == {
        "duplicate": 2,
        "generic": 1,
        "no_context_keyword": 1,
    }


def test_실제_쓰기가_있으면_기준선이_경고한다(conn: sqlite3.Connection):
    creator_id = _creator(conn, "a")
    media_pk = _media(conn, "AAA111", creator_id=creator_id)
    action_id = _action(conn, media_pk=media_pk, creator_id=creator_id, run_id="run-1")
    _interaction(
        conn,
        action_id=action_id,
        media_pk=media_pk,
        creator_id=creator_id,
        executor="browser",
        dry_run=False,
    )

    snapshot = collect_baseline(conn)

    assert snapshot.actions.real_writes == 1
    assert any("실제 Instagram 쓰기" in note for note in snapshot.notes)


def test_사람_feedback이_없으면_기준선이_알려준다(conn: sqlite3.Connection):
    snapshot = collect_baseline(conn)

    assert snapshot.actions.human_feedback == 0
    assert any("사람 Feedback" in note for note in snapshot.notes)


# ===========================================================================
# 18D.1 — Audit Trail
# ===========================================================================
@pytest.fixture()
def audit_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    """감사 추적이 다뤄야 하는 5가지 경우를 한 DB에 만든다.

    `interactions`에는 (media_pk, action_type, dry_run) UNIQUE가 걸려 있다
    (같은 게시물에 같은 동작을 두 번 하지 않게 막는 가드). 그래서 경우마다
    서로 다른 게시물을 쓴다 — 실제 운영에서도 그렇게 된다.
    """
    creator_id = _creator(conn, "office_daily_kim")
    media = {code: _media(conn, code, creator_id=creator_id) for code in
             ("AAA111", "BBB222", "CCC333", "DDD444", "EEE555")}
    conn.execute(
        """INSERT INTO autopilot_runs (run_id, started_at, status, mode, executor_mode)
           VALUES ('auto-1', '2026-10-01', 'OK', 'AUTOPILOT', 'DRY_RUN')"""
    )

    # 1) 운영자 Queue → browser DRY_RUN 실행
    one = _action(conn, media_pk=media["AAA111"], creator_id=creator_id, run_id="run-1")
    approve_action(conn, one, by=APPROVED_BY_OPERATOR)
    _interaction(
        conn, action_id=one, media_pk=media["AAA111"], creator_id=creator_id,
        executor="browser", dry_run=True,
    )
    _finish(conn, one, "SUCCESS")

    # 2) Autopilot Queue → browser DRY_RUN 실행
    two = _action(conn, media_pk=media["BBB222"], creator_id=creator_id, run_id="auto-1",
                  action_type="COMMENT", executor="browser")
    approve_action(conn, two, by=APPROVED_BY_AUTOPILOT)
    _interaction(
        conn, action_id=two, media_pk=media["BBB222"], creator_id=creator_id,
        executor="browser", dry_run=True, action_type="COMMENT",
    )
    _finish(conn, two, "SUCCESS")

    # 3) 운영자 Queue → manual executor 실행(예정과 실제가 같은 경우)
    three = _action(conn, media_pk=media["CCC333"], creator_id=creator_id, run_id="run-2")
    approve_action(conn, three, by=APPROVED_BY_OPERATOR)
    _interaction(
        conn, action_id=three, media_pk=media["CCC333"], creator_id=creator_id,
        executor="manual", dry_run=True,
    )
    _finish(conn, three, "SUCCESS")

    # 4) 실행 실패
    four = _action(conn, media_pk=media["DDD444"], creator_id=creator_id, run_id="run-3")
    approve_action(conn, four, by=APPROVED_BY_OPERATOR)
    _interaction(
        conn, action_id=four, media_pk=media["DDD444"], creator_id=creator_id,
        executor="browser", dry_run=True, success=False,
    )
    _finish(conn, four, "FAILED", error="selector_mismatch")

    # 5) 승인 전 취소(실행 기록 없음)
    five = _action(conn, media_pk=media["EEE555"], creator_id=creator_id, run_id="run-4",
                   status="CANCELLED")
    conn.execute(
        "UPDATE action_queue SET finished_at = '2026-10-01T00:00:01+00:00' WHERE action_id = ?",
        (five,),
    )
    return conn


def test_감사기록이_생성_승인_실행주체를_구분한다(audit_db: sqlite3.Connection):
    report = collect_audit(audit_db)
    by_id = {trace.action_id: trace for trace in report.traces}

    assert by_id[1].created_by == CREATED_BY_PIPELINE
    assert by_id[1].approved_by == APPROVED_BY_OPERATOR
    assert by_id[2].created_by == CREATED_BY_AUTOPILOT  # run_id가 autopilot_runs에 있다
    assert by_id[2].approved_by == APPROVED_BY_AUTOPILOT


def test_예정_executor와_실제_executor를_나눠_본다(audit_db: sqlite3.Connection):
    """action_queue.executor는 '예정', interactions.executor는 '실제'다.

    Autopilot은 Queue 생성과 무관하게 Browser Executor로 돌기 때문에 둘이 다를 수
    있고, 그건 오류가 아니다 — 같은 칸에 섞어 쓰지만 않으면 된다.
    """
    report = collect_audit(audit_db)
    by_id = {trace.action_id: trace for trace in report.traces}

    assert by_id[1].planned_executor == "manual"
    assert by_id[1].actual_executor == "browser"
    assert by_id[3].planned_executor == "manual" and by_id[3].actual_executor == "manual"
    # 1·4는 manual로 예정했지만 browser가 실행했다. 2는 예정도 실제도 browser다.
    assert report.executor_mismatches == 2
    assert report.consistent, report.issues  # 차이는 '감사 공백'이 아니다


def test_실행하지_않은_action은_실제_executor가_비어_있다(audit_db: sqlite3.Connection):
    report = collect_audit(audit_db)
    cancelled = next(trace for trace in report.traces if trace.status == "CANCELLED")

    assert cancelled.actual_executor == NOT_EXECUTED
    assert cancelled.actual_dry_run is None
    assert cancelled.executed is False
    assert cancelled.real_write is False


def test_dry_run_실행은_실제_쓰기로_세지_않는다(audit_db: sqlite3.Connection):
    report = collect_audit(audit_db)

    assert report.executed == 4
    assert report.real_writes == 0
    assert all(trace.real_write is False for trace in report.traces)


def test_실패한_action은_사유가_남는다(audit_db: sqlite3.Connection):
    report = collect_audit(audit_db)
    failed = next(trace for trace in report.traces if trace.status == "FAILED")

    assert failed.error == "selector_mismatch"
    assert report.consistent, report.issues


def test_실행했는데_승인기록이_없으면_감사공백으로_잡는다(conn: sqlite3.Connection):
    creator_id = _creator(conn, "a")
    media_pk = _media(conn, "AAA111", creator_id=creator_id)
    action_id = _action(conn, media_pk=media_pk, creator_id=creator_id, run_id="run-1")
    _interaction(
        conn, action_id=action_id, media_pk=media_pk, creator_id=creator_id,
        executor="browser", dry_run=True,
    )
    _finish(conn, action_id, "SUCCESS")  # approve_action을 거치지 않았다

    report = collect_audit(conn)

    assert report.unattributed_approvals == 1
    assert not report.consistent
    assert any("승인 주체" in issue for issue in report.issues)


def test_상태는_성공인데_실행기록이_없으면_감사공백이다(conn: sqlite3.Connection):
    creator_id = _creator(conn, "a")
    media_pk = _media(conn, "AAA111", creator_id=creator_id)
    action_id = _action(conn, media_pk=media_pk, creator_id=creator_id, run_id="run-1")
    approve_action(conn, action_id, by=APPROVED_BY_OPERATOR)
    _finish(conn, action_id, "SUCCESS")  # interactions 없음

    report = collect_audit(conn)

    assert not report.consistent
    assert any("실행 기록" in issue for issue in report.issues)


def test_승인_주체_미기록은_UNKNOWN으로_표시된다(conn: sqlite3.Connection):
    creator_id = _creator(conn, "a")
    media_pk = _media(conn, "AAA111", creator_id=creator_id)
    _action(conn, media_pk=media_pk, creator_id=creator_id, run_id="run-1")

    report = collect_audit(conn)

    assert report.traces[0].approved_by == UNKNOWN
    assert report.unattributed_approvals == 0  # PENDING은 아직 승인 단계가 아니다


def test_action에_연결되지_않은_실행기록은_감사공백이다(conn: sqlite3.Connection):
    """누가 시킨 건지 되짚을 수 없는 실행 기록은 그냥 넘기지 않는다.

    Action을 따라가며 세면 이런 기록은 '없는 것'처럼 보인다 — 실제 쓰기가
    있었는데도 0건으로 보고하게 된다.
    """
    creator_id = _creator(conn, "a")
    media_pk = _media(conn, "AAA111", creator_id=creator_id)
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_id, action_type, executor, dry_run, success, executed_at)
           VALUES (?, ?, NULL, 'LIKE', 'browser', 0, 1, '2026-10-01T00:00:00+00:00')""",
        (media_pk, creator_id),
    )

    report = collect_audit(conn)

    assert report.orphan_interactions == 1
    assert report.real_writes == 1  # Action이 없어도 쓰기는 쓰기다
    assert not report.consistent
    assert any("연결돼 있지 않습니다" in issue for issue in report.issues)
