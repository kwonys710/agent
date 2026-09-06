"""SQLite Machine State 연결/트랜잭션 헬퍼.

Checkpoint 원칙(Architecture v3.1):
"처리 성공 전 checkpoint 이동 금지" — 이를 코드로 강제하기 위해
하나의 실행(변경 수집~Registry 반영~checkpoint 갱신)을 단일 SQLite 트랜잭션으로 묶는다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from .schema import SCHEMA_STATEMENTS

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "pm_automation.db"


def get_connection(db_path: Union[Path, str] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """블록 안의 모든 쓰기가 성공해야 commit, 하나라도 예외가 나면 전부 rollback."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
