"""Runtime 실행/차단 관측 (P1-B).

LOCKED(차단)·멈춘 소유자·Canary 정지는 run 테이블에 행이 없어 DB에서 보이지 않았다.
기존 `app_state`(set_state/get_state)에 런타임당 키 2개만 남긴다 — 새 테이블·schema 변경 없음.

    runtime_run:<runtime>    lock **소유자만** 쓴다 (시작 / 종료)
    runtime_block:<runtime>  **차단당한 쪽만** 쓴다

키를 나눈 이유: 소유자와 차단된 프로세스가 같은 행을 read-modify-write 하면 lost update 로
started_at 이 지워져 멈춘 소유자가 숨는다. 합치지 않는다.

이 모듈은 관측만 한다. lock 을 회수하거나 프로세스를 죽이지 않고, 집계 테이블(run 테이블)을
건드리지 않으며, 기록 실패가 Run 을 실패로 만들지 않는다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.config import Config
from ..core.database import get_connection, get_state, set_state
from ..core.exceptions import DatabaseError
from ..core.logger import get_logger

logger = get_logger("scheduler.runtime_state")

RUN_KEY = "runtime_run:"
BLOCK_KEY = "runtime_block:"

# 연속 차단 경고 기준. 단발 LOCKED 는 수동 실행과 예약 실행이 겹친 정상 상황이라 경고하지 않는다.
BLOCK_STREAK_WARNING = 2

# runtime -> (표시 이름, 기존 lock.stale_minutes 키, runner 의 기본값). 새 config 키는 만들지 않는다.
RUNTIMES: dict[str, tuple[str, str, int]] = {
    "scheduler": ("Scheduler", "scheduler.lock.stale_minutes", 60),
    "browser_discovery": ("Browser Discovery", "browser_discovery.lock.stale_minutes", 30),
    "autopilot": ("Autopilot", "autopilot.lock.stale_minutes", 60),
    "comment_canary": ("COMMENT Canary", "canary.comment.lock.stale_minutes", 30),
}

NO_RECORD = "기록 없음"
NO_BLOCK = "차단 없음(0회)"


def _iso(now: Optional[datetime]) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _parse(raw: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    raw = get_state(conn, key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _save(conn: sqlite3.Connection, key: str, value: dict) -> None:
    set_state(conn, key, json.dumps(value, ensure_ascii=False))
    conn.commit()  # WAL 에서 다른 프로세스가 보려면 commit 이 필요하다


# --- 쓰기 (실패해도 Run 을 실패로 만들지 않는다) -------------------------------
def record_started(
    conn: Optional[sqlite3.Connection], runtime: str, lock: Any, *, run_id: str = "",
    now: Optional[datetime] = None,
) -> None:
    """lock 을 얻고 conn 을 확보한 직후. 소유자만 호출한다."""
    if conn is None:
        return
    try:
        _save(conn, RUN_KEY + runtime, {
            "runtime": runtime, "pid": os.getpid(), "run_id": run_id,
            "started_at": _iso(now), "lock_mode": lock.mode, "lock_path": str(lock.path),
            "finished_at": None, "status": None, "reason": None,
        })
    except sqlite3.Error:
        logger.warning("Runtime 시작 기록 실패(무시): %s", runtime)


def record_finished(
    conn: Optional[sqlite3.Connection], runtime: str, status: str, reason: str = "",
    *, now: Optional[datetime] = None,
) -> None:
    """finally 에서. 이 프로세스가 쓴 시작 기록만 갱신한다."""
    if conn is None:
        return
    try:
        run = _load(conn, RUN_KEY + runtime)
        if run is None or run.get("pid") != os.getpid():
            return
        run.update(finished_at=_iso(now), status=str(status), reason=str(reason or "") or None)
        _save(conn, RUN_KEY + runtime, run)
    except sqlite3.Error:
        logger.warning("Runtime 종료 기록 실패(무시): %s", runtime)


def record_blocked(
    config: Config, runtime: str, lock: Any, *, conn: Optional[sqlite3.Connection] = None,
    now: Optional[datetime] = None,
) -> None:
    """acquire() 가 False 일 때. conn 이 없으면 짧게 열고 닫는다. init_db 는 부르지 않는다."""
    own = conn is None
    try:
        if own:
            if not config.db_path.exists():  # 차단당한 실행이 DB 를 새로 만들지 않는다
                return
            conn = get_connection(config.db_path)
        stamp = _iso(now)
        prev = _load(conn, BLOCK_KEY + runtime)
        owner = _load(conn, RUN_KEY + runtime)
        owner_start = _parse(owner.get("started_at")) if owner else None
        prev_at = _parse(prev.get("blocked_at")) if prev else None
        # 직전 차단이 소유자의 마지막 시작보다 새로우면 같은 소유자에게 계속 막히는 중이다.
        cont = prev_at is not None and (owner_start is None or prev_at > owner_start)
        info = lock.blocked_info or {}
        _save(conn, BLOCK_KEY + runtime, {
            "runtime": runtime, "blocked_at": stamp,
            "first_blocked_at": (prev.get("first_blocked_at") or stamp) if cont else stamp,
            "streak": int(prev.get("streak") or 0) + 1 if cont else 1,
            "status": "LOCKED", "lock_mode": info.get("mode"), "lock_path": info.get("path"),
            "owner_pid": info.get("owner_pid"), "owner_created_at": info.get("owner_created_at"),
            "stale_minutes": info.get("stale_minutes"), "recovery": info.get("recovery"),
        })
    except (sqlite3.Error, DatabaseError, OSError):  # 테이블이 없으면 OperationalError 도 여기로 온다
        logger.warning("Runtime 차단 기록 실패(무시): %s", runtime)
    finally:
        if own and conn is not None:
            conn.close()


# --- 읽기 (표시 기준은 이 함수 하나다) -------------------------------------------
@dataclass
class RuntimeStatus:
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def block_summary(self) -> str:
        """상태바용 한 줄. 표본 없음과 실제 0 을 구분한다."""
        if not any(r["has_record"] for r in self.rows):
            return NO_RECORD
        parts = [f"{r['label']} {r['streak']}회" for r in self.rows if r["streak"]]
        return ", ".join(parts) if parts else NO_BLOCK


def _short(raw: Any) -> str:
    return str(raw)[5:16].replace("T", " ")


def collect_runtime_status(
    conn: sqlite3.Connection, config: Config, *, now: Optional[datetime] = None
) -> RuntimeStatus:
    now = now or datetime.now(timezone.utc)
    out = RuntimeStatus()
    for runtime, (label, key, default) in RUNTIMES.items():
        try:
            run, block = _load(conn, RUN_KEY + runtime), _load(conn, BLOCK_KEY + runtime)
        except sqlite3.Error:  # app_state 가 아직 없는 DB
            run = block = None
        stale = int(config.get(key, default))
        started = _parse(run.get("started_at")) if run else None
        blocked = _parse(block.get("blocked_at")) if block else None
        # 이후 소유자 실행이 시작됐으면 그 차단 연속은 끝난 것이다.
        streak = int(block.get("streak") or 0) if block and blocked and (
            started is None or blocked > started) else 0
        unfinished = bool(run and started and not run.get("finished_at"))
        minutes = int((now - started).total_seconds() // 60) if unfinished else 0
        suspect = unfinished and minutes >= stale

        if not run and not block:
            text = NO_RECORD
        else:
            last = (f"마지막 {run.get('status')} {_short(run.get('finished_at'))}"
                    if run and run.get("finished_at") else "마지막 완료 기록 없음")
            if streak:
                blk = f"차단 {streak}회(연속)"
            elif block:
                blk = f"최근 차단 {_short(block.get('blocked_at'))}(이후 실행 있음)"
            else:
                blk = NO_BLOCK
            if suspect:
                unf = f"미종료(멈춤 또는 비정상 종료) 의심 pid={run.get('pid')} {minutes}분 경과"
            elif unfinished:
                unf = f"실행 중 또는 미종료 {minutes}분 경과"
            else:
                unf = "미종료 없음"
            text = f"{last} · {blk} · {unf}"
        out.rows.append({"runtime": runtime, "label": label, "text": text, "streak": streak,
                         "suspect": suspect, "has_record": bool(run or block)})
        if streak >= BLOCK_STREAK_WARNING:
            out.warnings.append(
                f"{label}: {streak}회 연속 차단됨(소유 pid={block.get('owner_pid')}, "
                f"lock={block.get('lock_path')}). {block.get('recovery') or ''}".strip())
        if suspect:
            out.warnings.append(
                f"{label}: 미종료(멈춤 또는 비정상 종료) 의심 — pid={run.get('pid')}가 "
                f"{_short(run.get('started_at'))}에 시작한 뒤 {minutes}분째 종료 기록이 없습니다"
                f"(stale 기준 {stale}분, mode={run.get('lock_mode')}, lock={run.get('lock_path')}).")
    return out
