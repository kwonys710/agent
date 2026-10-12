"""운영 품질 모니터링 / Weekly Report 테스트 (Phase 18C.1).

모두 DB만 읽는다. LLM 호출 없음, Instagram 접속 없음.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from targeting_agent.core.config import Config
from targeting_agent.dashboard import queries
from targeting_agent.learning.operations import (
    collect_discovery_metrics,
    collect_executor_metrics,
    collect_operations_report,
    score_distribution,
)
from targeting_agent.learning.weekly_report import (
    build_weekly_report,
    render_weekly_html,
    write_weekly_report,
)


def insert_discovery_run(conn: sqlite3.Connection, **values) -> None:
    row = {
        "started_at": "2026-10-01T00:00:00+00:00",
        "finished_at": "2026-10-01T00:01:00+00:00",
        "status": "SUCCESS",
        "session_state": "LOGGED_IN",
        "queries": 3,
        "found": 9,
        "collected": 6,
        "added": 4,
        "duplicate": 2,
        "invalid": 0,
        "analyzed": 4,
        "needs_enrichment": 0,
        "claude_calls": 0,
        "cache_hits": 0,
        "errors": 0,
        "stop_reason": None,
        "username_found": 6,
        "caption_found": 5,
        "detail_failed": 0,
        **values,
    }
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO browser_discovery_runs ({columns}) VALUES ({marks})", tuple(row.values())
    )
    conn.commit()


def insert_autopilot_run(conn: sqlite3.Connection, **values) -> None:
    row = {
        "run_id": "r1",
        "started_at": "2026-10-01T00:00:00+00:00",
        "finished_at": "2026-10-01T00:05:00+00:00",
        "status": "OK",
        "mode": "REVIEW",
        "executor_mode": "DRY_RUN",
        "analyzed": 3,
        "queued_likes": 2,
        "queued_comments": 1,
        "approved": 0,
        "executed_success": 0,
        "executed_skipped": 0,
        "executed_failed": 0,
        "real_writes": 0,
        "claude_calls": 0,
        "cache_hits": 0,
        "errors": 0,
        "halt_reason": None,
        **values,
    }
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO autopilot_runs ({columns}) VALUES ({marks})", tuple(row.values()))
    conn.commit()


# --- Discovery 지표 --------------------------------------------------------
def test_discovery_추출률을_집계한다(conn: sqlite3.Connection):
    insert_discovery_run(conn)

    metrics = collect_discovery_metrics(conn)

    assert metrics.runs == 1
    assert metrics.collected == 6
    assert metrics.username_rate == 1.0
    assert round(metrics.caption_rate or 0, 2) == 0.83
    assert metrics.duplicate_rate == 2 / 6


def test_실패한_discovery_run을_센다(conn: sqlite3.Connection):
    insert_discovery_run(conn, status="FAILED", errors=3, collected=0, added=0, duplicate=0)

    metrics = collect_discovery_metrics(conn)

    assert metrics.failed_runs == 1
    assert metrics.selector_errors == 3
    assert metrics.selector_failure_rate == 1.0  # 검색어 3개 중 3개 실패


def test_기간을_주면_그_이후만_센다(conn: sqlite3.Connection):
    insert_discovery_run(conn, started_at="2026-09-01T00:00:00+00:00")
    insert_discovery_run(conn, started_at="2026-10-05T00:00:00+00:00")

    assert collect_discovery_metrics(conn, since="2026-10-01").runs == 1


# --- Executor 지표 ---------------------------------------------------------
def test_autopilot_중단을_실행_건강으로_센다(conn: sqlite3.Connection):
    insert_autopilot_run(conn, status="STOPPED", halt_reason="ACTION_BLOCKED")

    metrics = collect_executor_metrics(conn)

    assert metrics.autopilot_runs == 1
    assert metrics.autopilot_halted_runs == 1


def test_실제_쓰기와_dry_run을_구분한다(config: Config, conn: sqlite3.Connection):
    from targeting_agent.core.database import record_interaction
    from targeting_agent.core.models import ActionType

    from .test_browser_executor import seed_candidate

    media_pk, creator_id = seed_candidate(conn)
    for dry in (True, False):
        record_interaction(
            conn,
            media_pk=media_pk,
            creator_id=creator_id,
            action_id=None,
            action_type=ActionType.LIKE,
            executor="browser",
            dry_run=dry,
            success=True,
        )
    conn.commit()

    metrics = collect_executor_metrics(conn)

    assert metrics.real_writes == 1
    assert metrics.dry_run_writes == 1


# --- 보고서 ----------------------------------------------------------------
def test_score_분포를_구간별로_센다(config: Config, conn: sqlite3.Connection):
    from .test_browser_executor import seed_candidate

    for index, score in enumerate((55.0, 72.0, 85.0, 95.0)):
        media_pk, _ = seed_candidate(conn, code=f"S{index:04d}", username=f"creator_{index}")
        conn.execute(
            "UPDATE candidate_media SET target_score = ? WHERE media_pk = ?", (score, media_pk)
        )
    conn.commit()

    distribution = score_distribution(conn)

    assert distribution["0-59"] == 1
    assert distribution["70-79"] == 1
    assert distribution["80-89"] == 1
    assert distribution["90-100"] == 1


def test_운영_보고서에_모든_섹션이_있다(config: Config, conn: sqlite3.Connection):
    insert_discovery_run(conn)
    insert_autopilot_run(conn)

    report = collect_operations_report(conn, config)
    sections = report.as_sections()

    assert set(sections) == {"Discovery / 추출", "Action 실행", "Score 분포", "Action 추천 분포"}
    assert "Username 추출률" in sections["Discovery / 추출"]


def test_selector_실패가_많으면_경고한다(config: Config, conn: sqlite3.Connection):
    for _ in range(4):
        insert_discovery_run(conn, status="FAILED", queries=3, errors=3, collected=0)

    report = collect_operations_report(conn, config)

    assert any("검색어 실패율" in note for note in report.notes)


def test_weekly_report는_llm없이_생성된다(config: Config, conn: sqlite3.Connection, tmp_path):
    insert_discovery_run(conn, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    insert_autopilot_run(conn, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    report = build_weekly_report(conn, config)
    text = render_weekly_html(report)

    assert "Weekly Optimization Report" in text
    assert "LLM 호출 없음" in text
    assert "Discovery / 추출" in text
    assert report.start_date < report.end_date


def test_weekly_report를_파일로_저장한다(config: Config, conn: sqlite3.Connection, tmp_path):
    raw = {
        **config.raw,
        "scheduler": {
            **config.raw["scheduler"],
            "summary": {**config.raw["scheduler"]["summary"], "output_dir": str(tmp_path)},
        },
    }
    cfg = Config(raw=raw, path=config.path, base_dir=tmp_path)
    report = build_weekly_report(conn, cfg)

    path = write_weekly_report(cfg, report, render_weekly_html(report))

    assert path.exists()
    assert (tmp_path / "weekly_latest.html").exists()


def test_임계값을_자동으로_바꾸지_않는다(config: Config, conn: sqlite3.Connection):
    before_like = config.get("actions.require_score_for_like")
    before_comment = config.get("actions.require_score_for_comment")

    build_weekly_report(conn, config)
    collect_operations_report(conn, config)

    assert config.get("actions.require_score_for_like") == before_like
    assert config.get("actions.require_score_for_comment") == before_comment


# --- Dashboard 상태 --------------------------------------------------------
def test_dashboard_운영_상태를_보여준다(config: Config, conn: sqlite3.Connection):
    insert_discovery_run(conn)
    insert_autopilot_run(conn)

    status = queries.operational_status(conn, config)

    assert status["mode"] == "REVIEW"
    assert status["executor_mode"] == "DRY_RUN"
    assert "신규 4" in status["discovery_last"]
    assert "실제 쓰기 0" in status["autopilot_last"]
    assert "LIKE" in status["usage"]


def test_autopilot을_켜면_상태가_바뀐다(config: Config, conn: sqlite3.Connection):
    raw = {**config.raw, "autopilot": {"enabled": True}, "browser_executor": {"mode": "LIVE"}}
    cfg = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    status = queries.operational_status(conn, cfg)

    assert status["mode"] == "AUTOPILOT"
    assert status["executor_mode"] == "LIVE"


def test_실행_기록이_없으면_그렇게_표시한다(config: Config, conn: sqlite3.Connection):
    status = queries.operational_status(conn, config)

    assert status["discovery_last"] == "실행 기록 없음"
    assert status["autopilot_last"] == "실행 기록 없음"


# --- Implicit Feedback (Phase 18C.1 / §39) ---------------------------------
def seed_analysis(conn: sqlite3.Connection, media_pk: int, topic: str = "직장인") -> None:
    """topic이 있는 분석 결과 한 건(학습 신호 집계용)."""
    import json

    conn.execute(
        "INSERT INTO media_analysis (media_pk, analyzer, analyzer_version, prompt_version, "
        "payload, analyzed_at) VALUES (?, 'heuristic', '1', '1', ?, datetime('now'))",
        (media_pk, json.dumps({"topics": [topic], "keywords": []}, ensure_ascii=False)),
    )
def test_운영자_승인은_학습_신호가_된다(config: Config, conn: sqlite3.Connection):
    from targeting_agent.core.database import APPROVED_BY_OPERATOR, approve_action, enqueue_action
    from targeting_agent.core.models import ActionType
    from targeting_agent.learning.topics import DEFAULT_SIGNALS, collect_signals

    from .test_browser_executor import seed_candidate

    media_pk, creator_id = seed_candidate(conn)
    seed_analysis(conn, media_pk)
    action_id = enqueue_action(
        conn,
        run_id="r1",
        media_pk=media_pk,
        creator_id=creator_id,
        action_type=ActionType.LIKE,
        target_score=90,
        priority=90,
        executor="browser",
        dry_run=True,
    )
    approve_action(conn, int(action_id), by=APPROVED_BY_OPERATOR)
    conn.commit()

    signals, _ = collect_signals(conn, DEFAULT_SIGNALS)

    assert "직장인" in signals


def test_autopilot_자동승인은_학습_신호가_아니다(config: Config, conn: sqlite3.Connection):
    """Agent가 스스로 승인한 Action으로 학습하면 자기 강화가 된다 — 제외한다."""
    from targeting_agent.core.database import APPROVED_BY_AUTOPILOT, approve_action, enqueue_action
    from targeting_agent.core.models import ActionType
    from targeting_agent.learning.topics import DEFAULT_SIGNALS, collect_signals

    from .test_browser_executor import seed_candidate

    media_pk, creator_id = seed_candidate(conn)
    seed_analysis(conn, media_pk)
    action_id = enqueue_action(
        conn,
        run_id="r1",
        media_pk=media_pk,
        creator_id=creator_id,
        action_type=ActionType.LIKE,
        target_score=90,
        priority=90,
        executor="browser",
        dry_run=True,
    )
    approve_action(conn, int(action_id), by=APPROVED_BY_AUTOPILOT)
    conn.commit()

    signals, _ = collect_signals(conn, DEFAULT_SIGNALS)

    assert signals == {}


def test_action_실패는_품질_신호가_아니라_실행_건강이다(config: Config, conn: sqlite3.Connection):
    """실패한 Action은 학습 신호로 쓰지 않고 실행 건강 지표로만 센다."""
    from targeting_agent.core.database import enqueue_action, update_action_status
    from targeting_agent.core.models import ActionStatus, ActionType
    from targeting_agent.learning.topics import DEFAULT_SIGNALS, collect_signals

    from .test_browser_executor import seed_candidate

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
    update_action_status(
        conn, int(action_id), ActionStatus.FAILED, error="like_not_confirmed", finished=True
    )
    conn.commit()

    signals, _ = collect_signals(conn, DEFAULT_SIGNALS)
    metrics = collect_executor_metrics(conn)

    assert signals == {}          # 품질 신호로 쓰지 않는다
    assert metrics.failed == 1    # 실행 건강 지표로 센다
