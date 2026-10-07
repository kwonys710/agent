"""Autopilot Orchestration 테스트 (Phase 18C).

실제 Instagram 접속·쓰기 없음. Discovery는 FakeBrowser, Action은 FakeActionPage로 대체한다.
ai.provider=heuristic(conftest)이므로 Claude CLI도 호출하지 않는다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from targeting_agent.autopilot.runner import (
    STATUS_LOCKED,
    STATUS_OK,
    STATUS_STOPPED,
    format_result,
    run_autopilot,
)
from targeting_agent.core.config import Config
from targeting_agent.core.models import ActionStatus
from targeting_agent.discovery.browser_models import PostDetail, SessionState

from .test_browser_discovery import FakeBrowser
from .test_browser_executor import FakeActionPage
from .test_discovery_pipeline_e2e import CAPTIONS, QUERIES, url_of


def discovery_browser() -> FakeBrowser:
    links = {query: [url_of(code)] for query, code in zip(QUERIES, CAPTIONS)}
    details = {
        url_of(code): PostDetail(
            permalink=url_of(code),
            username=f"creator_{code.lower()}",
            caption=caption,
            media_type="REEL",
            like_count=300,
            comment_count=12,
        )
        for code, caption in CAPTIONS.items()
    }
    return FakeBrowser(links=links, details=details)


def autopilot_config(config: Config, *, autopilot: bool = False, **overrides) -> Config:
    # 이 테스트는 "오케스트레이션"을 보는 것이므로, 점수 기준은 낮춰 Action이 생기게 한다.
    # (Score 계산 자체는 Phase 3/17 테스트가 검증한다.)
    raw = {
        **config.raw,
        "scoring": {**config.raw["scoring"], "minimum_target_score": 10},
        "actions": {
            **config.raw["actions"],
            "require_score_for_like": 10,
            "require_score_for_comment": 20,
        },
        "executor": {**config.raw.get("executor", {}), "mode": "browser"},
        "browser_discovery": {
            "enabled": True,
            "headless": True,
            "queries": QUERIES,
            "media_types": ["REEL"],
            "limits": {
                "max_queries_per_run": 3,
                "max_candidates_per_query": 1,
                "max_candidates_per_run": 3,
                "max_analyze_per_run": 3,
            },
            "lock": {"enabled": False},
            "debug": {"enabled": False},
        },
        "browser_executor": {"mode": overrides.pop("executor_mode", "DRY_RUN")},
        "autopilot": {
            "enabled": autopilot,
            "discovery": True,
            "process_inbox": False,
            "max_candidates_per_run": 5,
            "max_approvals_per_run": 10,
            "execute_actions": True,
            "lock": {"enabled": False},
            "summary": {"enabled": False},
            **overrides,
        },
    }
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def live_autopilot_config(config: Config, **overrides) -> Config:
    """LIVE Executor + 실제 쓰기 허용 설정(쓰기는 FakeActionPage가 받는다)."""
    cfg = autopilot_config(config, autopilot=True, executor_mode="LIVE", **overrides)
    raw = {
        **cfg.raw,
        "actions": {
            **cfg.raw["actions"],
            "execution": {**cfg.raw["actions"]["execution"], "dry_run": False},
        },
    }
    return Config(raw=raw, path=cfg.path, base_dir=cfg.base_dir)


# --- 기본 흐름 -------------------------------------------------------------
def test_review_모드는_승인_없이_끝난다(config: Config, conn: sqlite3.Connection):
    page = FakeActionPage()
    result = run_autopilot(
        autopilot_config(config), conn, browser=discovery_browser(), action_page=page
    )

    assert result.status == STATUS_OK
    assert result.mode == "REVIEW"
    assert result.analyzed >= 1
    assert result.approved == 0          # 자동 승인 없음(Approval Gate 유지)
    assert result.executed_success == 0  # 승인된 Action이 없으므로 실행 대상도 없음
    assert result.real_writes == 0
    assert page.likes == 0 and page.comments == []


def test_autopilot_모드는_승인하고_dry_run으로_실행한다(
    config: Config, conn: sqlite3.Connection
):
    page = FakeActionPage()
    result = run_autopilot(
        autopilot_config(config, autopilot=True),
        conn,
        browser=discovery_browser(),
        action_page=page,
    )

    assert result.mode == "AUTOPILOT"
    assert result.queued_likes >= 1
    assert result.approved >= 1
    assert result.executed_success == result.approved
    # DRY_RUN이므로 브라우저 쓰기는 0건
    assert result.real_writes == 0
    assert page.likes == 0 and page.comments == []
    assert result.executor_mode == "DRY_RUN"


def test_live_모드에서_승인된_action이_실제_경로를_탄다(
    config: Config, conn: sqlite3.Connection
):
    page = FakeActionPage()
    result = run_autopilot(live_autopilot_config(config), conn, browser=discovery_browser(), action_page=page)

    assert result.executor_mode == "LIVE"
    assert result.executed_success >= 1
    assert page.likes >= 1               # 브라우저 LIKE 경로가 호출됐다
    assert result.real_writes >= 1       # dry_run=0 Interaction이 기록됐다
    assert page.opened                   # 게시물 화면을 열었다


def test_end_to_end_단계가_모두_기록된다(config: Config, conn: sqlite3.Connection):
    result = run_autopilot(
        autopilot_config(config, autopilot=True),
        conn,
        browser=discovery_browser(),
        action_page=FakeActionPage(),
    )

    stages = " ".join(result.stages)
    for expected in ("discovery:", "analyze:", "queue:", "approve:", "execute:"):
        assert expected in stages
    text = format_result(result)
    assert "Autopilot" in text and "AUTOPILOT" in text


def test_실행_이력이_남는다(config: Config, conn: sqlite3.Connection):
    result = run_autopilot(
        autopilot_config(config, autopilot=True),
        conn,
        browser=discovery_browser(),
        action_page=FakeActionPage(),
    )

    row = conn.execute(
        "SELECT run_id, status, mode, executor_mode, approved, real_writes "
        "FROM autopilot_runs ORDER BY autopilot_run_id DESC LIMIT 1"
    ).fetchone()
    assert row["run_id"] == result.run_id
    assert row["mode"] == "AUTOPILOT"
    assert row["executor_mode"] == "DRY_RUN"
    assert row["real_writes"] == 0


# --- 안전 ------------------------------------------------------------------
def test_discovery_세션_중단이면_전체를_멈춘다(config: Config, conn: sqlite3.Connection):
    page = FakeActionPage()
    result = run_autopilot(
        autopilot_config(config, autopilot=True),
        conn,
        browser=FakeBrowser(state=SessionState.CHALLENGE),
        action_page=page,
    )

    assert result.status == STATUS_STOPPED
    assert result.halt_reason == SessionState.CHALLENGE.value
    assert result.approved == 0
    assert page.likes == 0
    assert result.exit_code == 1


def test_action_차단이_감지되면_실행을_멈춘다(config: Config, conn: sqlite3.Connection):
    from targeting_agent.actions.executors.browser_page import STOP_ACTION_BLOCKED

    page = FakeActionPage(stop=STOP_ACTION_BLOCKED)
    result = run_autopilot(live_autopilot_config(config), conn, browser=discovery_browser(), action_page=page)

    assert result.halted is True
    assert result.status == STATUS_STOPPED
    assert page.likes == 0  # 차단 상태에서는 누르지 않는다


def test_lock이_잡혀_있으면_종료한다(config: Config, conn: sqlite3.Connection, tmp_path):
    lock_path = tmp_path / ".autopilot.lock"
    lock_path.write_text(
        '{"pid": 1, "created_at": "%s"}'
        % datetime.now(timezone.utc).isoformat(timespec="seconds"),
        encoding="utf-8",
    )
    cfg = autopilot_config(
        config, autopilot=True, lock={"enabled": True, "path": str(lock_path), "stale_minutes": 60}
    )

    result = run_autopilot(cfg, conn, browser=discovery_browser(), action_page=FakeActionPage())

    assert result.status == STATUS_LOCKED
    assert result.analyzed == 0


def test_executor_단계를_끄면_실행하지_않는다(config: Config, conn: sqlite3.Connection):
    page = FakeActionPage()
    result = run_autopilot(
        autopilot_config(config, autopilot=True, execute_actions=False),
        conn,
        browser=discovery_browser(),
        action_page=page,
    )

    assert result.approved >= 1          # 승인은 되지만
    assert result.executed_success == 0  # 실행 단계는 건너뛴다
    assert "execute:skipped(설정)" in result.stages


def test_discovery를_끄면_기존_후보만_처리한다(config: Config, conn: sqlite3.Connection):
    result = run_autopilot(
        autopilot_config(config, autopilot=True, discovery=False),
        conn,
        browser=discovery_browser(),
        action_page=FakeActionPage(),
    )

    assert result.discovery is None
    assert "discovery:skipped(설정)" in result.stages


def test_claude_한도를_우회하지_않는다(config: Config, conn: sqlite3.Connection):
    """heuristic provider 환경에서는 Claude 호출이 0이어야 한다."""
    result = run_autopilot(
        autopilot_config(config, autopilot=True),
        conn,
        browser=discovery_browser(),
        action_page=FakeActionPage(),
    )

    assert result.claude_calls == 0


# --- Scheduler 연동 --------------------------------------------------------
def test_scheduler가_autopilot을_위임할_수_있다(config: Config, conn: sqlite3.Connection):
    """scheduler.autopilot=true면 Scheduled Run이 Autopilot을 실행한다(기본은 false)."""
    from targeting_agent.scheduler.runner import run_scheduled

    cfg = autopilot_config(config, autopilot=True, discovery=False)
    raw = {
        **cfg.raw,
        "scheduler": {**cfg.raw["scheduler"], "autopilot": True, "summary": {"enabled": False}},
    }
    result = run_scheduled(Config(raw=raw, path=cfg.path, base_dir=cfg.base_dir), conn)

    assert result.status == "OK"
    assert "autopilot:" in result.message
    assert "mode=AUTOPILOT" in result.message


def test_scheduler_기본값은_autopilot을_쓰지_않는다(config: Config):
    assert bool(config.get("scheduler.autopilot", False)) is False
