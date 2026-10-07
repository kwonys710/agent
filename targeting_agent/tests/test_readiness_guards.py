"""Pre-Live Guardrails 테스트 (Phase 18D.5).

여기 있는 테스트는 **실제 Instagram 쓰기를 막는 장치**를 고정한다.
하나라도 깨지면 LIVE에서 의도하지 않은 동작이 나간다.
"""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.core.config import Config
from targeting_agent.core.models import ActionType, QueuedAction
from targeting_agent.readiness.guards import (
    EFFECTIVE_DISABLED,
    EFFECTIVE_DRY_RUN,
    EFFECTIVE_LIVE,
    FAIL,
    PASS,
    WARN,
    collect_guards,
    effective_mode,
)


def _config(base: Config, **overrides) -> Config:
    """config를 복사해 일부만 바꾼다(원본 불변)."""
    raw = {key: (dict(value) if isinstance(value, dict) else value) for key, value in base.raw.items()}
    for dotted, value in overrides.items():
        parts = dotted.split("__")
        node = raw
        for part in parts[:-1]:
            node[part] = dict(node.get(part, {}))
            node = node[part]
        node[parts[-1]] = value
    return Config(raw=raw, path=base.path, base_dir=base.base_dir)


def _queued(action_type: ActionType, **overrides) -> QueuedAction:
    """Executor에 넘길 최소 Action."""
    return QueuedAction(
        action_id=1,
        media_pk=1,
        media_id="AAA111",
        permalink="https://www.instagram.com/reel/AAA111/",
        username="office_daily_kim",
        creator_id=1,
        action_type=action_type,
        target_score=90.0,
        priority=90,
        **overrides,
    )


# ===========================================================================
# 이중 안전 스위치 — 두 개가 모두 풀려야 실제 쓰기가 일어난다
# ===========================================================================
@pytest.mark.parametrize(
    "mode, dry_run, expected",
    [
        ("DRY_RUN", True, EFFECTIVE_DRY_RUN),
        ("DRY_RUN", False, EFFECTIVE_DRY_RUN),   # mode가 안전하면 dry_run을 풀어도 쓰기 없음
        ("LIVE", True, EFFECTIVE_DRY_RUN),       # dry_run이 켜져 있으면 LIVE여도 쓰기 없음
        ("LIVE", False, EFFECTIVE_LIVE),         # 둘 다 풀렸을 때만 LIVE
        ("DISABLED", False, EFFECTIVE_DISABLED),
        ("DISABLED", True, EFFECTIVE_DISABLED),
    ],
)
def test_두_스위치가_모두_풀려야_live다(config: Config, mode: str, dry_run: bool, expected: str):
    target = _config(
        config,
        browser_executor__mode=mode,
        actions__execution__dry_run=dry_run,
    )

    assert effective_mode(target).effective == expected


def test_설정값과_실제동작을_따로_보여준다(config: Config):
    """LIVE로 설정했는데 실제로는 안 나가는 상태를 한 칸에 적으면 오해한다."""
    target = _config(
        config, browser_executor__mode="LIVE", actions__execution__dry_run=True
    )

    mode = effective_mode(target)

    assert mode.configured == "LIVE"
    assert mode.effective == EFFECTIVE_DRY_RUN
    assert "dry_run" in mode.reason
    assert "Configured: LIVE" in mode.as_line() and "Effective: DRY_RUN" in mode.as_line()


@pytest.mark.parametrize("value", ["", "live", "Live", "ENABLED", "yes", None])
def test_알_수_없는_모드는_안전한_쪽으로_본다(config: Config, value):
    """오타 하나로 쓰기가 열리면 안 된다. 다만 대소문자는 정상으로 받는다."""
    target = _config(
        config, browser_executor__mode=value, actions__execution__dry_run=False
    )

    mode = effective_mode(target)

    if str(value or "").strip().upper() == "LIVE":
        assert mode.effective == EFFECTIVE_LIVE  # 'live'/'Live'는 LIVE로 인정한다
    else:
        assert mode.effective == EFFECTIVE_DRY_RUN


# ===========================================================================
# Autopilot / Scheduler — 자기들만으로 쓰기를 열 수 없다
# ===========================================================================
def test_autopilot만_켜서는_쓰기가_열리지_않는다(config: Config):
    target = _config(
        config,
        autopilot__enabled=True,
        browser_executor__mode="DRY_RUN",
        actions__execution__dry_run=True,
    )

    assert effective_mode(target).writes_enabled is False


def test_scheduler_autopilot만_켜서는_쓰기가_열리지_않는다(config: Config):
    target = _config(
        config,
        scheduler__autopilot=True,
        browser_executor__mode="DRY_RUN",
        actions__execution__dry_run=True,
    )
    report = collect_guards(target)

    assert effective_mode(target).writes_enabled is False
    assert next(c for c in report.checks if c.name == "Scheduler LIVE 가드").status == PASS


def test_세_스위치가_모두_풀려야_autopilot이_실제로_쓴다(config: Config):
    target = _config(
        config,
        autopilot__enabled=True,
        scheduler__autopilot=True,
        browser_executor__mode="LIVE",
        actions__execution__dry_run=False,
    )
    report = collect_guards(target)

    assert effective_mode(target).writes_enabled is True
    # 위험 상태는 조용히 통과시키지 않는다.
    assert next(c for c in report.checks if c.name == "이중 안전 스위치").status == WARN
    assert next(c for c in report.checks if c.name == "Autopilot 자동 승인").status == WARN


# ===========================================================================
# Browser Executor 실제 동작 — DRY_RUN은 브라우저를 열지 않는다
# ===========================================================================
def test_dry_run이면_executor가_live가_아니다(config: Config):
    from targeting_agent.actions.executors.browser import BrowserExecutor

    executor = BrowserExecutor(dry_run=True, config=_config(config, browser_executor__mode="LIVE"))

    assert executor.live is False
    assert executor.validate_session() is True  # 브라우저를 열지 않고 통과


def test_mode가_live여도_dry_run이면_실제_동작이_없다(config: Config):
    from targeting_agent.actions.executors.browser import BrowserExecutor

    executor = BrowserExecutor(
        dry_run=True, config=_config(config, browser_executor__mode="LIVE"), page=None
    )
    action = _queued(ActionType.LIKE)

    result = executor.execute_like(action)

    assert result.dry_run is True
    assert executor._page is None  # 브라우저를 연 적이 없다


def test_댓글_내용이_없으면_comment를_실행하지_않는다(config: Config):
    """내용 없는 댓글이 나가는 것은 어떤 설정에서도 일어나면 안 된다."""
    from targeting_agent.actions.executors.browser import BrowserExecutor

    executor = BrowserExecutor(dry_run=True, config=config)
    action = _queued(ActionType.COMMENT, comment_text="   ")

    result = executor.execute_comment(action)

    assert result.success is False


# ===========================================================================
# 한도 / 중단 조건
# ===========================================================================
def test_기본_설정은_모든_가드를_통과한다(config: Config):
    report = collect_guards(config)

    assert report.passed, [check.name for check in report.failures]


def test_한도가_0이면_실패로_잡는다(config: Config):
    target = _config(config, actions__daily_limits={"likes": 0, "comments": 8})

    report = collect_guards(target)

    assert not report.passed
    assert any("LIKE 한도" in check.name for check in report.failures)


@pytest.mark.parametrize(
    "key",
    [
        "skip_private_accounts",
        "skip_ads",
        "skip_sensitive_content",
        "skip_if_already_interacted",
        "skip_unresolved_creator",
        "block_duplicate_comments",
        "halt_on_platform_warning",
    ],
)
def test_안전_옵션을_끄면_실패로_잡는다(config: Config, key: str):
    safety = {**config.raw["safety"], key: False}
    target = _config(config, safety=safety)

    report = collect_guards(target)

    assert not report.passed
    assert any(check.status == FAIL for check in report.checks)


def test_실제_쓰기_기록이_있으면_경고한다(conn: sqlite3.Connection, config: Config):
    conn.execute(
        "INSERT INTO creators (username, status, first_seen_at, updated_at) "
        "VALUES ('a', 'NEW', '2026-10-01', '2026-10-01')"
    )
    conn.execute(
        """INSERT INTO candidate_media
           (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
            source, status, discovered_at, updated_at)
           VALUES ('AAA111', 1, 'p', 'p', 'REEL', '', '[]', 'x', 'NEW', '2026-10-01', '2026-10-01')"""
    )
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_type, executor, dry_run, success, executed_at)
           VALUES (1, 1, 'LIKE', 'browser', 0, 1, '2026-10-01T00:00:00+00:00')"""
    )

    report = collect_guards(config, conn)

    check = next(c for c in report.checks if c.name == "실제 Instagram 쓰기 기록")
    assert check.status == WARN and "1건" in check.detail


def test_dry_run_기록만_있으면_통과한다(conn: sqlite3.Connection, config: Config):
    report = collect_guards(config, conn)

    check = next(c for c in report.checks if c.name == "실제 Instagram 쓰기 기록")
    assert check.status == PASS and "0건" in check.detail
