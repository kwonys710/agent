"""Runtime 실행/차단 관측 테스트 (P1-B).

LOCKED·멈춘 소유자·Canary 정지가 `app_state` 에서 보이는지, 그리고 그 기록이
run 테이블(집계 대상)과 Run 의 성공/실패를 건드리지 않는지 본다. 실제 Instagram 접속 없음.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from targeting_agent.actions.activity import (
    CAUTION_SIGNALS,
    STOP_SIGNALS,
    ActivityState,
    current_state,
    extract_signal,
    observe,
)
from targeting_agent.autopilot.runner import STATUS_LOCKED, run_autopilot
from targeting_agent.canary.comment_runner import (
    OUTCOME_BLOCKED_ACTIVITY,
    OUTCOME_LOCKED,
    OUTCOME_STOPPED,
    run_comment_canary,
)
from targeting_agent.canary.comment_executor import HALT_UNKNOWN
from targeting_agent.core.config import Config
from targeting_agent.core.database import get_connection, get_state, init_db
from targeting_agent.discovery.browser_runner import STATUS_LOCKED as DISCOVERY_LOCKED
from targeting_agent.discovery.browser_runner import run_browser_discovery
from targeting_agent.scheduler import runtime_state
from targeting_agent.scheduler.lock import SchedulerLock
from targeting_agent.scheduler.runner import STATUS_LOCKED as SCHEDULER_LOCKED
from targeting_agent.scheduler.runner import run_scheduled
from targeting_agent.scheduler.runtime_state import (
    BLOCK_STREAK_WARNING,
    NO_BLOCK,
    NO_RECORD,
    collect_runtime_status,
    record_blocked,
    record_finished,
    record_started,
)

from .test_autopilot import FakeActionPage, autopilot_config, discovery_browser, live_autopilot_config
from .test_comment_canary import CommentPage, canary_config, queue_comment
from .test_scheduler import _config as scheduler_config
from .test_scheduler import _holder, _run

T0 = datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)


def state(conn: sqlite3.Connection, key: str) -> dict:
    raw = get_state(conn, key)
    assert raw is not None, f"{key} 기록이 없다"
    return json.loads(raw)


def runtime_config(config: Config, tmp_path: Path) -> Config:
    """4개 Runtime 의 lock 을 켜고, DB 경로를 임시 파일로 돌린다."""
    raw = {
        **config.raw,
        "paths": {**config.raw.get("paths", {}), "database": str(tmp_path / "rt.db")},
        "scheduler": {**config.raw.get("scheduler", {}), "enabled": True,
                      "lock": {"enabled": True, "path": str(tmp_path / ".scheduler.lock"),
                               "stale_minutes": 60}},
        "autopilot": {**config.raw.get("autopilot", {}),
                      "lock": {"enabled": True, "path": str(tmp_path / ".autopilot.lock"),
                               "stale_minutes": 60}},
        "browser_discovery": {**config.raw.get("browser_discovery", {}), "enabled": True,
                              "lock": {"enabled": True, "path": str(tmp_path / ".bd.lock"),
                                       "stale_minutes": 30}},
        "canary": {**config.raw.get("canary", {}),
                   "comment": {**config.raw.get("canary", {}).get("comment", {}),
                               "lock": {"enabled": True, "path": str(tmp_path / ".cc.lock"),
                                        "stale_minutes": 30}}},
    }
    return Config(raw=raw, path=config.path, base_dir=tmp_path)


# runtime -> (lock 파일, label, 실행 함수(cfg, conn), run 테이블)
RUNTIMES = {
    "scheduler": (".scheduler.lock", "Scheduler Run",
                  lambda cfg, conn, now: run_scheduled(cfg, conn, now=now), "scheduled_runs"),
    "browser_discovery": (".bd.lock", "Browser Discovery",
                          lambda cfg, conn, now: run_browser_discovery(cfg, conn, now=now),
                          "browser_discovery_runs"),
    "autopilot": (".autopilot.lock", "Autopilot",
                  lambda cfg, conn, now: run_autopilot(cfg, conn, now=now), "autopilot_runs"),
    "comment_canary": (".cc.lock", "COMMENT Canary",
                       lambda cfg, conn, now: run_comment_canary(cfg, conn=conn, live=False, now=now),
                       None),
}


def rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


# --- 1. 차단 기록 ---------------------------------------------------------------
@pytest.mark.parametrize("runtime", list(RUNTIMES))
def test_차단되면_block_키에_소유자와_복구_안내가_남고_run_테이블은_그대로다(
    runtime, config, conn, tmp_path
):
    cfg = runtime_config(config, tmp_path)
    lock_file, label, run, table = RUNTIMES[runtime]
    before = {t: rows(conn, t) for t in ("scheduled_runs", "browser_discovery_runs", "autopilot_runs")}

    with _holder(tmp_path / lock_file, label=label) as child:
        outcome = run(cfg, conn, T0)

    assert outcome.exit_code == 0                      # LOCKED 는 여전히 정상 종료다
    block = state(conn, "runtime_block:" + runtime)
    assert block["status"] == "LOCKED" and block["streak"] == 1
    assert block["owner_pid"] == child.pid             # 막은 쪽의 pid (자기 pid 가 아니다)
    assert block["lock_mode"] == "KERNEL"
    assert block["lock_path"] == str(tmp_path / lock_file)
    assert "지우지 마십시오" in block["recovery"]
    assert block["stale_minutes"] in (30, 60)
    assert block["owner_created_at"]
    assert block["blocked_at"] == T0.isoformat(timespec="seconds")
    # 차단당한 쪽은 run 키를 쓰지 않는다(소유자 기록을 덮지 않는다)
    assert get_state(conn, "runtime_run:" + runtime) is None
    # LOCKED 행을 run 테이블에 넣지 않는다 → 집계가 바뀌지 않는다
    assert {t: rows(conn, t) for t in before} == before


def test_conn_없이_차단되면_짧게_열어_기록하고_닫는다(config, tmp_path):
    cfg = runtime_config(config, tmp_path)
    setup = get_connection(cfg.db_path)
    init_db(setup)
    setup.close()

    with _holder(tmp_path / ".scheduler.lock") as child:
        assert run_scheduled(cfg, now=T0).status == SCHEDULER_LOCKED

    check = get_connection(cfg.db_path)
    try:
        assert state(check, "runtime_block:scheduler")["owner_pid"] == child.pid
    finally:
        check.close()


def test_차단당한_실행은_DB를_새로_만들지_않는다(config, tmp_path):
    cfg = runtime_config(config, tmp_path)
    assert not cfg.db_path.exists()

    with _holder(tmp_path / ".scheduler.lock"):
        assert run_scheduled(cfg, now=T0).status == SCHEDULER_LOCKED

    assert not cfg.db_path.exists()                    # init_db/connect 부작용 없음


def test_테이블이_없는_DB에서도_차단은_조용히_끝난다(config, tmp_path):
    cfg = runtime_config(config, tmp_path)
    bare = get_connection(cfg.db_path)                 # init_db 를 하지 않은 DB
    bare.close()

    with _holder(tmp_path / ".scheduler.lock"):
        result = run_scheduled(cfg, now=T0)

    assert result.status == SCHEDULER_LOCKED and result.exit_code == 0
    check = get_connection(cfg.db_path)
    try:
        tables = {r["name"] for r in check.execute("SELECT name FROM sqlite_master")}
        assert "app_state" not in tables                # 만들지 않았다
    finally:
        check.close()


# --- 2. streak ------------------------------------------------------------------
def fake_lock(pid: int = 4242) -> SimpleNamespace:
    return SimpleNamespace(
        mode="KERNEL", path=Path("/x/.lock"),
        blocked_info={"mode": "KERNEL", "path": "/x/.lock", "owner_pid": pid,
                      "owner_created_at": T0.isoformat(), "stale_minutes": 30,
                      "recovery": "프로세스를 종료하면 즉시 해제됩니다."},
    )


def test_연속_차단은_streak이_늘고_정상_실행이_끼면_1로_리셋된다(config, conn, tmp_path):
    cfg = runtime_config(config, tmp_path)
    lock = fake_lock()
    record_started(conn, "autopilot", lock, now=T0)                         # 소유자 시작
    record_blocked(cfg, "autopilot", lock, conn=conn, now=T0 + timedelta(minutes=1))
    assert state(conn, "runtime_block:autopilot")["streak"] == 1
    record_blocked(cfg, "autopilot", lock, conn=conn, now=T0 + timedelta(minutes=2))
    second = state(conn, "runtime_block:autopilot")
    assert second["streak"] == BLOCK_STREAK_WARNING == 2
    assert second["first_blocked_at"] == (T0 + timedelta(minutes=1)).isoformat(timespec="seconds")

    # 그 사이에 정상 실행이 끼었다(= 새 소유자가 시작했다)
    record_started(conn, "autopilot", lock, now=T0 + timedelta(minutes=10))
    record_blocked(cfg, "autopilot", lock, conn=conn, now=T0 + timedelta(minutes=11))
    third = state(conn, "runtime_block:autopilot")
    assert third["streak"] == 1
    assert third["first_blocked_at"] == third["blocked_at"]


def test_러너_경로에서도_연속_차단이_누적된다(config, conn, tmp_path):
    cfg = runtime_config(config, tmp_path)
    with _holder(tmp_path / ".scheduler.lock"):
        run_scheduled(cfg, conn, now=T0)
        run_scheduled(cfg, conn, now=T0 + timedelta(minutes=5))

    assert state(conn, "runtime_block:scheduler")["streak"] == 2


def test_소유자_기록은_차단_기록이_덮지_않는다(config, conn, tmp_path):
    """키를 나눈 이유: 차단된 쪽의 쓰기가 소유자의 started_at 을 지우면 멈춘 소유자가 숨는다."""
    cfg = runtime_config(config, tmp_path)
    lock = fake_lock()
    record_started(conn, "scheduler", lock, now=T0)
    for minute in (1, 2, 3):
        record_blocked(cfg, "scheduler", lock, conn=conn, now=T0 + timedelta(minutes=minute))

    run = state(conn, "runtime_run:scheduler")
    assert run["started_at"] == T0.isoformat(timespec="seconds") and run["finished_at"] is None


# --- 3. 정상 실행 ---------------------------------------------------------------
def test_정상_실행은_시작과_종료를_기록한다(config, conn, tmp_path):
    cfg = scheduler_config(config, tmp_path)

    result, _ = _run(conn, cfg, now=T0)
    assert result.status == "OK"
    run = state(conn, "runtime_run:scheduler")

    assert run["pid"] == os.getpid()
    assert run["started_at"] == T0.isoformat(timespec="seconds")
    assert run["finished_at"] and run["status"] == "OK"
    assert run["lock_mode"] == "KERNEL"
    assert run["lock_path"] == str(tmp_path / ".scheduler.lock")
    assert get_state(conn, "runtime_block:scheduler") is None


def test_예외로_끝난_실행도_종료가_기록되고_scheduled_runs_의미는_그대로다(
    config, conn, tmp_path, monkeypatch
):
    """finally 에서 닫는다. 단 scheduled_runs 의 _record_run 위치는 옮기지 않았다(KPI 불변)."""
    from targeting_agent.scheduler import runner

    cfg = scheduler_config(config, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("summary 실패")

    monkeypatch.setattr(runner, "_write_summary", boom)
    result, _ = _run(conn, cfg, now=T0)

    assert result.status == "FAILED"
    run = state(conn, "runtime_run:scheduler")
    assert run["status"] == "FAILED" and run["finished_at"] and "summary 실패" in run["reason"]
    assert rows(conn, "scheduled_runs") == 0           # 예전과 같다: try 마지막 줄이라 남지 않는다


# --- 4. 미종료 판정 경계 --------------------------------------------------------
@pytest.mark.parametrize(
    "runtime, stale", [("scheduler", 60), ("browser_discovery", 30), ("autopilot", 60),
                       ("comment_canary", 30)],
)
def test_미종료는_stale_minutes_이상일_때만_경고한다(config, conn, runtime, stale):
    record_started(conn, runtime, fake_lock(), now=T0)

    just_before = collect_runtime_status(conn, config, now=T0 + timedelta(minutes=stale, seconds=-1))
    exactly = collect_runtime_status(conn, config, now=T0 + timedelta(minutes=stale))

    assert just_before.warnings == []
    assert len(exactly.warnings) == 1
    assert "미종료(멈춤 또는 비정상 종료)" in exactly.warnings[0]
    assert f"{stale}분" in exactly.warnings[0]
    row = next(r for r in exactly.rows if r["runtime"] == runtime)
    assert row["suspect"] is True


def test_종료가_기록된_실행은_오래돼도_미종료가_아니다(config, conn):
    record_started(conn, "scheduler", fake_lock(), now=T0)
    record_finished(conn, "scheduler", "OK", now=T0 + timedelta(minutes=1))

    status = collect_runtime_status(conn, config, now=T0 + timedelta(days=3))

    assert status.warnings == []
    assert "미종료 없음" in next(r for r in status.rows if r["runtime"] == "scheduler")["text"]


def test_단발_차단은_경고하지_않고_연속_차단부터_경고한다(config, conn, tmp_path):
    cfg = runtime_config(config, tmp_path)
    record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0)
    assert collect_runtime_status(conn, config, now=T0).warnings == []

    record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0 + timedelta(minutes=1))
    warnings = collect_runtime_status(conn, config, now=T0).warnings
    assert len(warnings) == 1
    assert "2회 연속" in warnings[0] and "pid=4242" in warnings[0]
    assert "프로세스를 종료하면 즉시 해제됩니다." in warnings[0]   # lock 이 만든 복구 문장 그대로


def test_이후_소유자가_시작하면_지난_연속_차단은_경고하지_않는다(config, conn, tmp_path):
    cfg = runtime_config(config, tmp_path)
    record_started(conn, "autopilot", fake_lock(), now=T0)
    record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0 + timedelta(minutes=1))
    record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0 + timedelta(minutes=2))
    record_started(conn, "autopilot", fake_lock(), now=T0 + timedelta(minutes=10))
    record_finished(conn, "autopilot", "OK", now=T0 + timedelta(minutes=11))

    status = collect_runtime_status(conn, config, now=T0 + timedelta(minutes=12))

    assert status.warnings == []
    assert "이후 실행 있음" in next(r for r in status.rows if r["runtime"] == "autopilot")["text"]


# --- 5. 표본 없음 vs 0 ----------------------------------------------------------
def test_기록이_없으면_기록_없음이고_경고도_없다(config, conn):
    status = collect_runtime_status(conn, config, now=T0)

    assert [r["text"] for r in status.rows] == [NO_RECORD] * 4
    assert status.warnings == []
    assert status.block_summary == NO_RECORD


def test_실행_기록은_있고_차단이_없으면_0회로_구분한다(config, conn):
    record_started(conn, "scheduler", fake_lock(), now=T0)
    record_finished(conn, "scheduler", "OK", now=T0 + timedelta(minutes=1))

    status = collect_runtime_status(conn, config, now=T0 + timedelta(minutes=2))

    assert status.block_summary == NO_BLOCK
    assert NO_BLOCK in next(r for r in status.rows if r["runtime"] == "scheduler")["text"]
    assert next(r for r in status.rows if r["runtime"] == "autopilot")["text"] == NO_RECORD


def test_app_state_테이블이_없어도_읽기는_기록_없음이다(config):
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row

    status = collect_runtime_status(bare, config, now=T0)

    assert status.block_summary == NO_RECORD and status.warnings == []


# --- 6. 기록 실패 격리 ----------------------------------------------------------
def test_기록이_실패해도_Run_결과와_exit_code는_그대로다(config, conn, tmp_path, monkeypatch):
    cfg = scheduler_config(config, tmp_path)
    baseline, _ = _run(conn, cfg, now=T0)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime_state, "set_state", broken)
    result, _ = _run(conn, cfg, now=T0 + timedelta(hours=1))

    assert result.status == baseline.status == "OK"
    assert result.exit_code == baseline.exit_code == 0
    assert rows(conn, "scheduled_runs") == 2           # 기존 이력은 그대로 쌓인다


def test_차단_기록이_실패해도_LOCKED_exit_0이다(config, conn, tmp_path, monkeypatch):
    cfg = runtime_config(config, tmp_path)
    setup = get_connection(cfg.db_path)
    init_db(setup)
    setup.close()

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(runtime_state, "get_connection", broken)
    with _holder(tmp_path / ".scheduler.lock"):
        result = run_scheduled(cfg, now=T0)

    assert result.status == SCHEDULER_LOCKED and result.exit_code == 0


def test_차단_기록이_conn으로_실패해도_LOCKED_exit_0이다(config, conn, tmp_path, monkeypatch):
    cfg = runtime_config(config, tmp_path)
    monkeypatch.setattr(runtime_state, "set_state",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with _holder(tmp_path / ".scheduler.lock"):
        result = run_scheduled(cfg, conn, now=T0)

    assert result.status == SCHEDULER_LOCKED and result.exit_code == 0


# --- 7. autopilot ---------------------------------------------------------------
def test_autopilot_기록에_run_id가_들어간다(config, conn, tmp_path):
    cfg = autopilot_config(
        config, autopilot=True,
        lock={"enabled": True, "path": str(tmp_path / ".autopilot.lock"), "stale_minutes": 60},
    )

    result = run_autopilot(cfg, conn, browser=discovery_browser(), action_page=FakeActionPage(), now=T0)

    run = state(conn, "runtime_run:autopilot")
    assert run["run_id"] == result.run_id and run["run_id"]
    assert run["finished_at"] and run["status"] == result.status
    assert conn.execute("SELECT run_id FROM autopilot_runs").fetchone()["run_id"] == result.run_id


def test_autopilot_LOCKED는_autopilot_runs에_행을_만들지_않는다(config, conn, tmp_path):
    lock_path = tmp_path / ".autopilot.lock"
    holder = SchedulerLock(path=lock_path, stale_minutes=60, label="Autopilot")
    assert holder.acquire() is True
    cfg = autopilot_config(
        config, autopilot=True, lock={"enabled": True, "path": str(lock_path), "stale_minutes": 60}
    )
    try:
        result = run_autopilot(cfg, conn, browser=discovery_browser(), action_page=FakeActionPage(), now=T0)
    finally:
        holder.release()

    assert result.status == STATUS_LOCKED
    assert rows(conn, "autopilot_runs") == 0
    assert state(conn, "runtime_block:autopilot")["streak"] == 1
    assert get_state(conn, "runtime_run:autopilot") is None


# --- 8. comment canary ----------------------------------------------------------
def test_comment_canary_BLOCKED_ACTIVITY가_DB에_남는다(config, conn):
    queue_comment(conn)
    observe(conn, "CHALLENGE", now=T0)

    result = run_comment_canary(canary_config(config), conn=conn, now=T0, page=CommentPage())

    assert result.outcome == OUTCOME_BLOCKED_ACTIVITY
    run = state(conn, "runtime_run:comment_canary")
    assert run["status"] == OUTCOME_BLOCKED_ACTIVITY
    assert run["reason"] == "activity_STOPPED" and run["finished_at"]


def test_comment_canary_STOPPED가_DB에_남는다(config, conn):
    from targeting_agent.actions.executors.browser_page import COMMENT_UNKNOWN_STATE

    queue_comment(conn)
    page = CommentPage(state=COMMENT_UNKNOWN_STATE, detail="not_in_comment_list")

    result = run_comment_canary(canary_config(config), conn=conn, now=T0, page=page)

    assert result.outcome == OUTCOME_STOPPED
    run = state(conn, "runtime_run:comment_canary")
    assert run["status"] == OUTCOME_STOPPED
    assert run["reason"].startswith(HALT_UNKNOWN)


def test_comment_canary_LOCKED도_차단으로_남는다(config, conn, tmp_path):
    cfg = runtime_config(config, tmp_path)
    with _holder(tmp_path / ".cc.lock", label="COMMENT Canary"):
        result = run_comment_canary(cfg, conn=conn, live=False, now=T0)

    assert result.outcome == OUTCOME_LOCKED
    assert state(conn, "runtime_block:comment_canary")["status"] == "LOCKED"


# --- 9. 전수 검사 ---------------------------------------------------------------
def _call_name(node: ast.Call):
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def test_SchedulerLock을_쓰는_모든_모듈이_차단_기록도_호출한다():
    """교훈 1: 한 호출부만 고치면 나머지에 같은 결함이 남는다. 미적용 집합을 정확히 고정한다.

    canary/resume.py 는 이번 Task 범위 밖(다른 Task 가 작업 중)이다. 그 파일에 차단 기록이
    들어가면 이 단정이 깨지고, 그때 이 집합에서 빼는 것이 후속 처리의 완료 표시다.
    """
    root = Path(__file__).resolve().parent.parent
    users: set[str] = set()
    recording: set[str] = set()
    lifecycle: dict[str, set[str]] = {}
    for source in sorted(root.rglob("*.py")):
        if "tests" in source.parts:
            continue
        rel = source.relative_to(root).as_posix()
        names = {_call_name(n) for n in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
                 if isinstance(n, ast.Call)}
        if "SchedulerLock" in names:
            users.add(rel)
        if "record_blocked" in names:
            recording.add(rel)
        lifecycle[rel] = names

    assert len(users) >= 5                              # 탐색 범위가 좁아지지 않았는지 자체 점검
    assert users - recording == {"canary/resume.py"}
    for rel in recording:
        assert {"record_started", "record_finished"} <= lifecycle[rel], rel


# --- 10. S2: 플랫폼 신호 분류 ---------------------------------------------------
@pytest.mark.parametrize(
    "reason, expected",
    [
        ("platform_warning:CHALLENGE:LIKE media 1", "CHALLENGE"),
        ("platform_warning:ACTION_BLOCKED:COMMENT media 2", "ACTION_BLOCKED"),
        ("platform_warning:LOGIN_REQUIRED:LIKE media 3", "LOGIN_REQUIRED"),
        ("CHALLENGE", "CHALLENGE"),
        ("platform_warning:PLATFORM_WARNING:LIKE media 1", "PLATFORM_WARNING"),
        ("platform_warning:인증 재확인 필요", "PLATFORM_WARNING"),
        ("UNKNOWN", "UNKNOWN"),
    ],
)
def test_래퍼_라벨이_안쪽_신호를_가리지_않는다(reason, expected):
    assert extract_signal(reason) == expected


def test_래퍼_안의_CAUTION_신호도_CAUTION으로_분류된다():
    assert extract_signal("platform_warning:TRY_AGAIN_LATER:x") in CAUTION_SIGNALS


def test_STOP과_CAUTION이_함께_있으면_STOP이_이긴다():
    assert extract_signal("platform_warning:TRY_AGAIN_LATER:CHALLENGE") == "CHALLENGE"
    assert extract_signal("PLATFORM_WARNING / ACTION_BLOCKED") == "ACTION_BLOCKED"


def test_UNKNOWN_COMMENT_STATE는_UNKNOWN_신호로_오인되지_않는다():
    assert extract_signal("UNKNOWN_COMMENT_STATE:not_in_comment_list") == "UNKNOWN_COMMENT_STATE"
    assert extract_signal("") == "" and extract_signal(None) == ""


@pytest.mark.parametrize("signal", sorted(STOP_SIGNALS))
def test_CHALLENGE류는_래핑돼도_STOPPED가_된다(conn, signal):
    assert observe(conn, extract_signal(f"platform_warning:{signal}:LIKE media 1"), now=T0) is ActivityState.STOPPED


def test_순수_platform_warning은_CAUTION을_유지한다(conn):
    assert observe(conn, extract_signal("platform_warning:LIKE media 1"), now=T0) is ActivityState.CAUTION


def test_observe는_악화_방향으로만_바꾼다(conn):
    observe(conn, extract_signal("platform_warning:CHALLENGE:x"), now=T0)
    assert current_state(conn)[0] is ActivityState.STOPPED

    # 이미 STOPPED 에서 더 약한 신호가 들어와도 풀리지 않는다
    assert observe(conn, extract_signal("platform_warning:PLATFORM_WARNING:x"), now=T0) is ActivityState.STOPPED
    assert observe(conn, "", now=T0) is ActivityState.STOPPED
    assert not (STOP_SIGNALS & CAUTION_SIGNALS)


def test_autopilot에서_CHALLENGE가_래핑돼_와도_STOPPED로_올린다(config, conn):
    """실제 호출 경로: Controller 가 'platform_warning:CHALLENGE:...' 로 감싼다."""
    page = FakeActionPage(stop_after_action="CHALLENGE")

    result = run_autopilot(live_autopilot_config(config), conn, browser=discovery_browser(), action_page=page)

    assert result.halted is True and result.halt_reason.startswith("platform_warning:CHALLENGE")
    assert result.activity_state == "STOPPED"
    assert current_state(conn)[0] is ActivityState.STOPPED
