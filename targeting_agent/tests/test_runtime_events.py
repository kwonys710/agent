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
    assert users - recording == {"canary/resume.py"}, (
        "차단 기록을 적용하지 않은 SchedulerLock 사용처가 예정과 다르다. canary/resume.py 에 "
        "record_blocked 를 넣었다면 이 집합에서 빼라 — record_started/record_finished 도 같이 넣어야 "
        "한다(아래 단정이 강제한다). 이 단정을 지우지 마라: 새 lock 사용처가 기록을 빠뜨리는 것을 막는다."
    )
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


# =============================================================================
# 리뷰 후속: 로그 · 보존 · 나이 상한 · 손상 입력 · 기본값 · 소유권 · 트랜잭션 · 토큰화
# =============================================================================
def runtime_row(status, runtime: str) -> dict:
    return next(r for r in status.rows if r["runtime"] == runtime)


# --- H1. 조기 반환도 흔적을 남긴다 ----------------------------------------------
def test_DB가_없어서_차단_기록을_생략하면_로그가_남는다(config, tmp_path, caplog):
    import logging

    cfg = runtime_config(config, tmp_path)
    assert not cfg.db_path.exists()

    with caplog.at_level(logging.INFO, logger="targeting_agent.scheduler.runtime_state"):
        record_blocked(cfg, "scheduler", fake_lock(), now=T0)

    assert "Runtime 차단 기록 생략" in caplog.text and "DB 없음" in caplog.text
    assert str(cfg.db_path) in caplog.text


# --- M1. 직전 완료 정보 보존 ----------------------------------------------------
def test_시작_기록이_직전_완료_정보를_보존한다(config, conn):
    record_started(conn, "scheduler", fake_lock(), now=T0)
    record_finished(conn, "scheduler", "FAILED", "boom", now=T0 + timedelta(minutes=1))
    record_started(conn, "scheduler", fake_lock(), now=T0 + timedelta(hours=1))

    run = state(conn, "runtime_run:scheduler")
    assert run["finished_at"] is None and run["status"] is None            # 이번 실행은 진행 중
    assert run["last_status"] == "FAILED" and run["last_reason"] == "boom"
    assert run["last_finished_at"] == (T0 + timedelta(minutes=1)).isoformat(timespec="seconds")
    text = runtime_row(collect_runtime_status(conn, config, now=T0 + timedelta(hours=1, minutes=5)),
                       "scheduler")["text"]
    assert "직전 완료 FAILED" in text and "실행 중 또는 미종료 5분 경과" in text


def test_종료_없이_다시_시작해도_마지막_완료는_이어진다(config, conn):
    """SIGKILL 로 끝난 실행 뒤에도 '직전 실행은 성공했나'에 답할 수 있어야 한다."""
    record_started(conn, "autopilot", fake_lock(), now=T0)
    record_finished(conn, "autopilot", "OK", now=T0 + timedelta(minutes=1))
    record_started(conn, "autopilot", fake_lock(), now=T0 + timedelta(hours=1))      # 이 실행은 죽었다
    record_started(conn, "autopilot", fake_lock(), now=T0 + timedelta(hours=2))      # 다음 실행

    run = state(conn, "runtime_run:autopilot")
    assert run["last_status"] == "OK"
    assert run["last_finished_at"] == (T0 + timedelta(minutes=1)).isoformat(timespec="seconds")


def test_첫_실행은_완료_기록_없음이다(config, conn):
    record_started(conn, "scheduler", fake_lock(), now=T0)

    run = state(conn, "runtime_run:scheduler")
    assert run["last_status"] is None and run["last_finished_at"] is None
    assert "완료 기록 없음" in runtime_row(
        collect_runtime_status(conn, config, now=T0 + timedelta(minutes=1)), "scheduler")["text"]


# --- M2. 연속 차단 경고의 나이 상한 ---------------------------------------------
@pytest.mark.parametrize("runtime, stale", [("scheduler", 60), ("comment_canary", 30)])
def test_연속_차단_경고는_stale_분의_2배까지만_유지된다(config, conn, tmp_path, runtime, stale):
    from targeting_agent.scheduler.runtime_state import BLOCK_WARNING_MAX_AGE_FACTOR

    cfg = runtime_config(config, tmp_path)
    last_block = T0 + timedelta(minutes=1)
    record_blocked(cfg, runtime, fake_lock(), conn=conn, now=T0)
    record_blocked(cfg, runtime, fake_lock(), conn=conn, now=last_block)
    limit = stale * BLOCK_WARNING_MAX_AGE_FACTOR

    at_limit = collect_runtime_status(conn, config, now=last_block + timedelta(minutes=limit))
    past = collect_runtime_status(conn, config, now=last_block + timedelta(minutes=limit, seconds=60))

    assert len(at_limit.warnings) == 1
    assert past.warnings == []                                   # 압력이 끝난 오래된 streak
    assert "차단 2회(연속" in runtime_row(past, runtime)["text"]   # 사실은 행에 남는다
    assert BLOCK_WARNING_MAX_AGE_FACTOR == 2


# --- M3. 같은 초 경계 · 손상 입력 -----------------------------------------------
def test_같은_초에_시작한_소유자를_반복_차단해도_streak이_쌓인다(config, conn, tmp_path):
    cfg = runtime_config(config, tmp_path)
    record_started(conn, "scheduler", fake_lock(), now=T0)
    record_blocked(cfg, "scheduler", fake_lock(), conn=conn, now=T0)
    record_blocked(cfg, "scheduler", fake_lock(), conn=conn, now=T0)

    assert state(conn, "runtime_block:scheduler")["streak"] == 2
    assert len(collect_runtime_status(conn, config, now=T0).warnings) == 1


def test_손상된_started_at은_연속으로_보지_않고_로그를_남긴다(config, conn, tmp_path, caplog):
    import logging

    cfg = runtime_config(config, tmp_path)
    record_started(conn, "autopilot", fake_lock(), now=T0)
    record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0 + timedelta(minutes=1))
    run = state(conn, "runtime_run:autopilot")
    run["started_at"] = "not-a-time"
    conn.execute("UPDATE app_state SET value = ? WHERE key = 'runtime_run:autopilot'",
                 (json.dumps(run),))
    conn.commit()

    with caplog.at_level(logging.WARNING, logger="targeting_agent.scheduler.runtime_state"):
        record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0 + timedelta(minutes=2))
        status = collect_runtime_status(conn, config, now=T0 + timedelta(minutes=3))

    assert state(conn, "runtime_block:autopilot")["streak"] == 1           # 누적하지 않는다(거짓 경고 방지)
    assert status.warnings == []
    assert "not-a-time" in caplog.text
    assert "시작 시각을 읽을 수 없음" in runtime_row(status, "autopilot")["text"]


def test_손상된_JSON은_기록_없음과_구분되게_로그를_남긴다(config, conn, caplog):
    import logging

    conn.execute("INSERT INTO app_state (key, value, updated_at) VALUES "
                 "('runtime_run:scheduler', '{broken', 'x')")
    conn.commit()

    with caplog.at_level(logging.WARNING, logger="targeting_agent.scheduler.runtime_state"):
        status = collect_runtime_status(conn, config, now=T0)

    assert "손상" in caplog.text and "runtime_run:scheduler" in caplog.text
    assert runtime_row(status, "scheduler")["text"] == NO_RECORD


# --- M4. stale 기본값은 한 곳 ---------------------------------------------------
def without_stale_keys(config: Config) -> Config:
    raw = json.loads(json.dumps(config.raw))
    for dotted in ("scheduler.lock", "autopilot.lock", "browser_discovery.lock", "canary.comment.lock"):
        node = raw
        for part in dotted.split("."):
            node = node.setdefault(part, {})
        node.pop("stale_minutes", None)
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


DEFAULT_STALE = {"scheduler": 60, "browser_discovery": 30, "autopilot": 60, "comment_canary": 30}


@pytest.mark.parametrize("runtime, stale", sorted(DEFAULT_STALE.items()))
def test_config에_stale_minutes가_없으면_기본값으로_판정한다(config, conn, runtime, stale):
    cfg = without_stale_keys(config)
    record_started(conn, runtime, fake_lock(), now=T0)

    before = collect_runtime_status(conn, cfg, now=T0 + timedelta(minutes=stale, seconds=-1))
    at = collect_runtime_status(conn, cfg, now=T0 + timedelta(minutes=stale))

    assert before.warnings == [] and len(at.warnings) == 1


def test_러너가_lock에_넘기는_stale_기본값은_readiness가_쓰는_값과_같다(config, conn, tmp_path, monkeypatch):
    """러너 기본값이 바뀌면 readiness 판정만 옛 값으로 남는 것을 막는다(기본값은 한 곳)."""
    from targeting_agent.autopilot import runner as autopilot_runner
    from targeting_agent.canary import comment_runner
    from targeting_agent.discovery import browser_runner
    from targeting_agent.scheduler import runner as scheduler_runner
    from targeting_agent.scheduler.runtime_state import lock_stale_minutes

    seen: dict[str, int] = {}

    class Recorder:
        def __init__(self, *, stale_minutes, label="Scheduled Run", **kwargs):
            self.stale_minutes, self.label = stale_minutes, label
            self.blocked_info, self.mode, self.path = {}, "", tmp_path / "x"

        def acquire(self, now=None):
            seen[self.label] = self.stale_minutes
            return False

    cfg = without_stale_keys(runtime_config(config, tmp_path))
    for module in (scheduler_runner, browser_runner, autopilot_runner, comment_runner):
        monkeypatch.setattr(module, "SchedulerLock", Recorder)
    run_scheduled(cfg, conn)
    run_browser_discovery(cfg, conn)
    run_autopilot(cfg, conn)
    run_comment_canary(cfg, conn=conn, live=False)

    assert seen == {
        "Scheduled Run": lock_stale_minutes(cfg, "scheduler"),
        "Browser Discovery": lock_stale_minutes(cfg, "browser_discovery"),
        "Autopilot": lock_stale_minutes(cfg, "autopilot"),
        "COMMENT Canary": lock_stale_minutes(cfg, "comment_canary"),
    }
    assert {k: lock_stale_minutes(cfg, k) for k in DEFAULT_STALE} == DEFAULT_STALE


# --- M5. record_finished 의 pid 소유권 가드 -------------------------------------
def test_다른_pid의_run_기록은_record_finished가_건드리지_않는다(config, conn, caplog):
    import logging

    record_started(conn, "scheduler", fake_lock(), now=T0)
    run = state(conn, "runtime_run:scheduler")
    run["pid"] = os.getpid() + 1                      # 나중에 시작한 다른 프로세스가 덮은 기록
    conn.execute("UPDATE app_state SET value = ? WHERE key = 'runtime_run:scheduler'", (json.dumps(run),))
    conn.commit()
    before = get_state(conn, "runtime_run:scheduler")

    with caplog.at_level(logging.DEBUG, logger="targeting_agent.scheduler.runtime_state"):
        record_finished(conn, "scheduler", "OK", now=T0 + timedelta(minutes=1))

    assert get_state(conn, "runtime_run:scheduler") == before
    assert "Runtime 종료 기록 생략" in caplog.text and f"pid={os.getpid() + 1}" in caplog.text


def test_run_기록이_없으면_record_finished는_만들지_않는다(conn):
    record_finished(conn, "scheduler", "OK", now=T0)

    assert get_state(conn, "runtime_run:scheduler") is None


# --- L3 / L4. 읽기 쪽 방어 ------------------------------------------------------
def test_stale_minutes가_잘못된_값이어도_읽기는_죽지_않고_기본값을_쓴다(config, conn, caplog):
    import logging

    from targeting_agent.dashboard import queries

    raw = json.loads(json.dumps(config.raw))
    raw["scheduler"]["lock"]["stale_minutes"] = "30m"
    cfg = Config(raw=raw, path=config.path, base_dir=config.base_dir)
    record_started(conn, "scheduler", fake_lock(), now=T0)

    with caplog.at_level(logging.WARNING, logger="targeting_agent.scheduler.runtime_state"):
        status = collect_runtime_status(conn, cfg, now=T0 + timedelta(minutes=60))
        queries.operational_status(conn, cfg)                # Dashboard 경로도 죽지 않는다

    assert len(status.warnings) == 1 and "기본값 60분" in caplog.text


def test_읽기_오류는_조용히_기록_없음이_되지_않고_로그를_남긴다(config, conn, monkeypatch, caplog):
    import logging

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime_state, "get_state", locked)
    with caplog.at_level(logging.WARNING, logger="targeting_agent.scheduler.runtime_state"):
        status = collect_runtime_status(conn, config, now=T0)

    assert "database is locked" in caplog.text
    assert status.block_summary == NO_RECORD


# --- H2. block_summary 의 구분 --------------------------------------------------
def test_block_summary는_과거_차단과_미종료_의심을_드러낸다(config, conn, tmp_path):
    cfg = runtime_config(config, tmp_path)
    # 과거에 차단됐고 지금은 끝난 경우 — '차단 없음(0회)' 로 사라지면 안 된다
    record_started(conn, "scheduler", fake_lock(), now=T0)
    for minute in (1, 2, 3):
        record_blocked(cfg, "scheduler", fake_lock(), conn=conn, now=T0 + timedelta(minutes=minute))
    record_finished(conn, "scheduler", "OK", now=T0 + timedelta(minutes=50))
    record_started(conn, "scheduler", fake_lock(), now=T0 + timedelta(hours=2))
    record_finished(conn, "scheduler", "OK", now=T0 + timedelta(hours=2, minutes=1))

    past = collect_runtime_status(conn, config, now=T0 + timedelta(hours=3))
    assert past.block_summary == "Scheduler Run 최근 차단 있음(연속 아님)"
    assert past.warnings == []

    # 아무도 더 시도하지 않는 멈춘 소유자 — 상태바에도 보여야 한다
    record_started(conn, "autopilot", fake_lock(), now=T0)
    stuck = collect_runtime_status(conn, config, now=T0 + timedelta(hours=10))
    assert "Autopilot 미종료 의심" in stuck.block_summary
    assert "Scheduler Run 최근 차단 있음(연속 아님)" in stuck.block_summary

    # 현재 연속 차단
    record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0 + timedelta(hours=9))
    record_blocked(cfg, "autopilot", fake_lock(), conn=conn, now=T0 + timedelta(hours=9, minutes=1))
    current = collect_runtime_status(conn, config, now=T0 + timedelta(hours=10))
    assert "Autopilot 차단 2회(연속)" in current.block_summary


# --- 판정 #2. 트랜잭션 결합 -----------------------------------------------------
def file_db_config(config: Config, tmp_path: Path, build) -> Config:
    cfg = build(Config(raw={**config.raw, "paths": {**config.raw.get("paths", {}),
                                                    "database": str(tmp_path / "tx.db")}},
                       path=config.path, base_dir=config.base_dir))
    return cfg


def test_comment_canary는_실패한_Run의_미커밋_승인을_영속시키지_않는다(config, tmp_path, monkeypatch):
    """record_finished 의 commit 이 approve 루프 도중 실패한 부분 쓰기를 살리면 안 된다."""
    from targeting_agent.canary import comment_runner

    cfg = file_db_config(config, tmp_path, lambda c: canary_config(c, limit=2))
    setup = get_connection(cfg.db_path)
    init_db(setup)
    queue_comment(setup, code="AAA111", username="creator_a")
    queue_comment(setup, code="BBB222", username="creator_b")
    setup.close()

    real, calls = comment_runner.approve_action, []

    def flaky(conn, action_id, **kwargs):
        calls.append(action_id)
        if len(calls) == 2:
            raise RuntimeError("두 번째 승인에서 실패")
        return real(conn, action_id, **kwargs)

    monkeypatch.setattr(comment_runner, "approve_action", flaky)

    result = run_comment_canary(cfg, now=T0, page=CommentPage())

    assert result.outcome == "FAILED" and len(calls) == 2
    check = get_connection(cfg.db_path)
    try:
        approved = check.execute("SELECT COUNT(*) AS n FROM action_queue WHERE approved_at IS NOT NULL")
        assert approved.fetchone()["n"] == 0                       # 첫 승인도 영속되지 않았다
        assert state(check, "runtime_run:comment_canary")["status"] == "FAILED"   # 기록은 남았다
    finally:
        check.close()


def test_comment_canary는_호출자_conn을_commit하지_않는다(config, conn, monkeypatch):
    from targeting_agent.canary import comment_runner

    queue_comment(conn)

    def fail_after_write(conn_, *args, **kwargs):
        conn_.execute("INSERT INTO app_state (key, value, updated_at) VALUES ('probe', '1', 'x')")
        raise RuntimeError("조회 실패")                         # Run 이 미커밋 쓰기를 남기고 실패

    monkeypatch.setattr(comment_runner, "eligible_comments", fail_after_write)

    result = run_comment_canary(canary_config(config), conn=conn, now=T0, page=CommentPage())

    assert result.outcome == "FAILED"
    assert state(conn, "runtime_run:comment_canary")["status"] == "FAILED"   # 같은 트랜잭션 안에서는 보인다
    conn.rollback()
    assert get_state(conn, "probe") is None                                  # 호출자의 쓰기를 대신 확정하지 않았다
    assert state(conn, "runtime_run:comment_canary")["status"] is None       # 기록은 호출자 트랜잭션에 합류했다


@pytest.mark.parametrize("runtime", ["scheduler", "browser_discovery", "autopilot"])
def test_나머지_세_runner는_실패한_Run의_부분_쓰기를_영속시킨다(config, tmp_path, monkeypatch, runtime):
    """의도된 결합을 고정한다: finally 의 commit 이 같은 conn 의 미커밋 쓰기를 확정한다.

    scheduler 는 예전에는 close 가 롤백했고(개선), discovery/autopilot 은 _record_run 이 이미 commit 했다.
    이 결합이 바뀌면(예: 기록을 별도 연결로 옮기면) 이 테스트가 알려 준다.
    """
    from targeting_agent.autopilot import runner as autopilot_runner
    from targeting_agent.discovery import browser_runner
    from targeting_agent.scheduler import runner as scheduler_runner

    cfg = runtime_config(config, tmp_path)

    def boom(*args, **kwargs):
        conn = next(a for a in args if isinstance(a, sqlite3.Connection))
        conn.execute("INSERT INTO app_state (key, value, updated_at) VALUES ('partial_probe', '1', 'x')")
        raise RuntimeError("중간 실패")

    target = {
        "scheduler": (scheduler_runner, "_write_summary", lambda: run_scheduled(cfg, now=T0)),
        "browser_discovery": (browser_runner, "_resolve_queries",
                              lambda: run_browser_discovery(cfg, now=T0)),
        "autopilot": (autopilot_runner, "_stage_expansion", lambda: run_autopilot(cfg, now=T0)),
    }[runtime]
    monkeypatch.setattr(target[0], target[1], boom)

    result = target[2]()

    assert result.exit_code == 1                              # 실패한 Run
    check = get_connection(cfg.db_path)
    try:
        assert get_state(check, "partial_probe") == "1"       # 부분 쓰기가 영속됐다(의도된 결합)
        assert state(check, f"runtime_run:{runtime}")["status"] == "FAILED"
    finally:
        check.close()


# --- 판정 #5. 산문 입력은 신호가 아니다 ----------------------------------------
@pytest.mark.parametrize(
    "reason, expected_state",
    [
        ("selector_missing: challenge-dialog not found", None),
        ("session ok, challenge=false", None),
        ("CHALLENGE 아님: platform_warning", ActivityState.CAUTION),     # 조각이 신호 이름과 같지 않다
        ("platform_warning:TRY_AGAIN_LATER / no challenge", ActivityState.CAUTION),
        ("no challenge detected", None),
        ("action blocked", None),                      # 공백 형태는 예전에도 매칭되지 않았다(생산자는 밑줄 형식)
        ("platform_warning:CHALLENGE:LIKE media 1", ActivityState.STOPPED),
        ("PLATFORM_WARNING / ACTION_BLOCKED", ActivityState.STOPPED),
        ("LOGIN_REQUIRED", ActivityState.STOPPED),
    ],
)
def test_산문_입력은_거짓_STOPPED를_만들지_않는다(conn, reason, expected_state):
    result = observe(conn, extract_signal(reason), now=T0)

    assert result is (expected_state or ActivityState.NORMAL), extract_signal(reason)
    assert extract_signal(reason) not in STOP_SIGNALS or expected_state is ActivityState.STOPPED
