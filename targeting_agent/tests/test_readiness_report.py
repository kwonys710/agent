"""Live Readiness 판정 / 보고서 테스트 (Phase 18D.7 · 18D.8).

LLM을 부르지 않는다 — 같은 DB·같은 설정이면 같은 보고서가 나와야 한다.
"""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.core.config import Config
from targeting_agent.core.database import APPROVED_BY_OPERATOR, approve_action
from targeting_agent.readiness.report import (
    CONDITIONAL_GO,
    GO,
    NO_GO,
    NOT_READY,
    READY,
    build_readiness,
    format_readiness,
    render_readiness_html,
    write_readiness_report,
)


def _config(base: Config, **overrides) -> Config:
    raw = {key: (dict(value) if isinstance(value, dict) else value) for key, value in base.raw.items()}
    for dotted, value in overrides.items():
        parts = dotted.split("__")
        node = raw
        for part in parts[:-1]:
            node[part] = dict(node.get(part, {}))
            node = node[part]
        node[parts[-1]] = value
    return Config(raw=raw, path=base.path, base_dir=base.base_dir)


def _healthy(conn: sqlite3.Connection, *, feedback: int = 0) -> None:
    """차단 사유가 없는 최소 상태를 만든다."""
    cursor = conn.execute(
        "INSERT INTO creators (username, followers, status, first_seen_at, updated_at) "
        "VALUES ('office_daily_kim', 5000, 'NEW', '2026-10-01', '2026-10-01')"
    )
    creator_id = int(cursor.lastrowid)
    cursor = conn.execute(
        """INSERT INTO candidate_media
           (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
            source, status, target_score, posted_at, discovered_at, updated_at)
           VALUES ('AAA111', ?, 'https://www.instagram.com/reel/AAA111/',
                   'https://www.instagram.com/reel/AAA111/', 'REEL', '퇴근길 기록', '[]',
                   'instagram_browser_search', 'SCORED', 90.0, '2026-10-01',
                   '2026-10-01', '2026-10-01')""",
        (creator_id,),
    )
    media_pk = int(cursor.lastrowid)
    conn.execute(
        """INSERT INTO media_analysis
           (media_pk, analyzer, analyzer_version, prompt_version, payload, similarity,
            target_score, score_breakdown, analyzed_at)
           VALUES (?, 'claude_code', '1', '1', '{}', 0.9, 90.0,
                   '{"components": {"content_similarity": 0.9, "creator_fit": 0.9,
                     "activity": 0.9, "engagement": 0.9, "language_region": 1.0,
                     "novelty": 1.0}, "weights": {"content_similarity": 0.35,
                     "creator_fit": 0.2, "activity": 0.15, "engagement": 0.15,
                     "language_region": 0.1, "novelty": 0.05}}', '2026-10-01')""",
        (media_pk,),
    )
    conn.execute(
        """INSERT INTO comment_drafts
           (media_pk, text, normalized_text, language, length, quality_ok, quality_reason,
            similarity_max, status, generator, generator_version, created_at)
           VALUES (?, '퇴근길 풍경 좋네요', '퇴근길 풍경 좋네요', 'ko', 10, 1, '', 0.0,
                   'SELECTED', 'claude_code', '1', '2026-10-01')""",
        (media_pk,),
    )
    for index in range(feedback):
        conn.execute(
            "INSERT INTO feedback (media_pk, creator_id, feedback_type, value, created_at) "
            "VALUES (?, ?, 'GOOD_TARGET', 1.0, ?)",
            (media_pk, creator_id, f"2026-10-{index + 1:02d}"),
        )


# ===========================================================================
# 판정
# ===========================================================================
def test_안전장치가_깨지면_다른_지표와_무관하게_no_go다(conn: sqlite3.Connection, config: Config):
    _healthy(conn, feedback=12)
    broken = _config(config, actions__daily_limits={"likes": 0, "comments": 8})

    report = build_readiness(conn, broken)

    assert report.verdict == NO_GO
    assert report.technical == NOT_READY
    assert any("안전장치 실패" in item for item in report.blockers)


def test_실제_쓰기가_기록돼_있으면_no_go다(conn: sqlite3.Connection, config: Config):
    """Phase 18D 동안 실제 쓰기는 0이어야 한다."""
    _healthy(conn, feedback=12)
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_type, executor, dry_run, success, executed_at)
           VALUES (1, 1, 'LIKE', 'browser', 0, 1, '2026-10-01T00:00:00+00:00')"""
    )

    report = build_readiness(conn, config)

    assert report.verdict == NO_GO
    assert any("실제 Instagram 쓰기" in item for item in report.blockers)


def test_감사_공백이_있으면_no_go다(conn: sqlite3.Connection, config: Config):
    _healthy(conn, feedback=12)
    conn.execute(
        """INSERT INTO action_queue
           (run_id, media_pk, creator_id, action_type, target_score, priority, status,
            executor, dry_run, created_at, updated_at)
           VALUES ('run-1', 1, 1, 'LIKE', 90.0, 90, 'SUCCESS', 'browser', 1,
                   '2026-10-01', '2026-10-01')"""
    )  # 실행 기록(interactions) 없이 SUCCESS

    report = build_readiness(conn, config)

    assert report.verdict == NO_GO
    assert any("감사 공백" in item for item in report.blockers)


def test_사람_feedback이_적으면_no_go가_아니라_conditional_go다(
    conn: sqlite3.Connection, config: Config
):
    """아직 배우지 못한 것은 망가진 것이 아니다(§45)."""
    _healthy(conn, feedback=1)

    report = build_readiness(conn, config)

    assert report.verdict == CONDITIONAL_GO
    assert report.technical == READY
    assert report.calibration_confidence == "LOW"


def test_feedback이_없어도_기술_준비도는_따로_본다(conn: sqlite3.Connection, config: Config):
    _healthy(conn, feedback=0)

    report = build_readiness(conn, config)

    assert report.technical == READY
    assert report.verdict != NO_GO
    assert report.calibration_confidence == "INSUFFICIENT_HUMAN_FEEDBACK"


def test_모두_갖춰지면_go다(conn: sqlite3.Connection, config: Config):
    _healthy(conn, feedback=12)

    report = build_readiness(conn, config)

    assert report.calibration_confidence == "HIGH"
    assert report.verdict == GO, report.risks
    assert not report.blockers


def test_사슬이_끊기면_no_go다(conn: sqlite3.Connection, config: Config):
    _healthy(conn, feedback=12)
    conn.execute(
        """INSERT INTO action_queue
           (run_id, media_pk, creator_id, action_type, comment_text, target_score, priority,
            status, executor, dry_run, created_at, updated_at)
           VALUES ('run-1', 1, 1, 'COMMENT', NULL, 90.0, 90, 'SUCCESS', 'browser', 1,
                   '2026-10-01', '2026-10-01')"""
    )
    approve_action(conn, 1, by=APPROVED_BY_OPERATOR)
    conn.execute(
        "UPDATE action_queue SET finished_at = '2026-10-01T00:00:01+00:00' WHERE action_id = 1"
    )
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_id, action_type, executor, dry_run, success, executed_at)
           VALUES (1, 1, 1, 'COMMENT', 'browser', 1, 1, '2026-10-01T00:00:00+00:00')"""
    )

    report = build_readiness(conn, config)

    assert report.verdict == NO_GO
    assert any("사슬 끊김" in item for item in report.blockers)


# ===========================================================================
# 보고서
# ===========================================================================
def test_같은_입력이면_같은_보고서가_나온다(conn: sqlite3.Connection, config: Config):
    """결정론적이어야 한다 — LLM을 부르지 않는다."""
    _healthy(conn, feedback=12)
    from datetime import datetime, timezone

    fixed = datetime(2026, 10, 7, tzinfo=timezone.utc)

    first = render_readiness_html(build_readiness(conn, config, now=fixed))
    second = render_readiness_html(build_readiness(conn, config, now=fixed))

    assert first == second


def test_보고서는_설정_상태를_그대로_보여준다(conn: sqlite3.Connection, config: Config):
    _healthy(conn)

    report = build_readiness(conn, config)

    assert report.config_state["browser_executor.mode"] == "DRY_RUN"
    assert report.config_state["actions.execution.dry_run"] == "true"
    assert report.config_state["autopilot.enabled"] == "false"
    assert "Effective: DRY_RUN" in report.config_state["Effective"]


def test_보고서는_전환_단계를_권고만_한다(conn: sqlite3.Connection, config: Config):
    _healthy(conn)

    report = build_readiness(conn, config)

    assert len(report.rollout) >= 4
    assert any("LIVE를 켜지 않습니다" in step for step in report.rollout)


def test_같은_위험을_두_번_적지_않는다(conn: sqlite3.Connection, config: Config):
    _healthy(conn)

    report = build_readiness(conn, config)

    assert len(report.risks) == len(set(report.risks))
    assert len(report.recommendations) == len(set(report.recommendations))


def test_html은_값을_escape한다(conn: sqlite3.Connection, config: Config):
    _healthy(conn)
    conn.execute(
        "UPDATE candidate_media SET source = '<script>alert(1)</script>' WHERE media_pk = 1"
    )

    text = render_readiness_html(build_readiness(conn, config))

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_보고서_파일과_latest를_남긴다(conn: sqlite3.Connection, config: Config, tmp_path):
    _healthy(conn)
    target = _config(config, paths={**config.raw["paths"], "data_dir": str(tmp_path)})
    target = _config(target, scheduler__summary={"enabled": True, "output_dir": str(tmp_path)})
    report = build_readiness(conn, target)

    path = write_readiness_report(target, report, render_readiness_html(report))

    assert path.exists()
    assert (path.parent / "live_readiness_latest.html").exists()


@pytest.mark.parametrize("verdict", [GO, CONDITIONAL_GO, NO_GO])
def test_터미널_요약에_판정이_보인다(verdict: str):
    from targeting_agent.readiness.report import ReadinessReport

    report = ReadinessReport(verdict=verdict, technical=READY)

    assert verdict in format_readiness(report)


def test_판정은_설정을_바꾸지_않는다(conn: sqlite3.Connection, config: Config):
    _healthy(conn)
    before = dict(config.raw["actions"]["execution"])

    build_readiness(conn, config)

    assert config.raw["actions"]["execution"] == before
    assert config.get("browser_executor.mode") == "DRY_RUN"
