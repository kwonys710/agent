"""WBS state schema scaffold 테스트 (Commit 1).

- 신규 WBS 테이블만 추가됐고 기존 Drive 테이블은 그대로인지
- internal_task_id / source_task_id identity 모델이 의도대로 동작하는지
  (source_task_id 중복 허용, internal_task_id PK 강제)
"""
import sqlite3

import pytest

from engine.state.database import get_connection, init_db

EXISTING_DRIVE_TABLES = {"source_checkpoints", "folders", "files", "processing_events"}
NEW_WBS_TABLES = {"wbs_task_state", "daily_todo_run", "daily_todo_item"}


def _table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _insert_task(conn, internal_task_id, source_task_id, row_key="WBS!R2"):
    conn.execute(
        """
        INSERT INTO wbs_task_state (
            internal_task_id, project_id, spreadsheet_id, sheet_name,
            source_task_id, row_key, task_name, row_hash, is_active
        ) VALUES (?, 'demo', 'SHEET', 'WBS', ?, ?, 'task', 'h0', 1)
        """,
        (internal_task_id, source_task_id, row_key),
    )


def test_new_wbs_tables_exist_after_init(conn):
    # H
    names = _table_names(conn)
    assert NEW_WBS_TABLES.issubset(names)


def test_existing_drive_tables_still_exist(conn):
    # I
    names = _table_names(conn)
    assert EXISTING_DRIVE_TABLES.issubset(names)


def test_processing_events_schema_unchanged(conn):
    # processing_events는 이번 작업에서 수정 금지 — drive_file_id 컬럼 그대로.
    cols = _columns(conn, "processing_events")
    assert cols == {
        "event_id",
        "project_id",
        "drive_file_id",
        "event_type",
        "detected_at",
        "details",
    }


def test_wbs_task_state_columns():
    conn = get_connection(":memory:")
    init_db(conn)
    try:
        cols = _columns(conn, "wbs_task_state")
        for expected in (
            "internal_task_id",
            "source_task_id",
            "source_identity_key",
            "row_key",
            "row_hash",
            "is_active",
        ):
            assert expected in cols
    finally:
        conn.close()


def test_duplicate_source_task_id_allowed_with_distinct_internal_ids(conn):
    # J: 동일 source_task_id를 가진 2개 row를 서로 다른 internal_task_id로 저장 가능해야 한다.
    _insert_task(conn, "int-1", "WBS-014", row_key="WBS!R15")
    _insert_task(conn, "int-2", "WBS-014", row_key="WBS!R16")
    conn.commit()

    rows = conn.execute(
        "SELECT internal_task_id FROM wbs_task_state WHERE source_task_id = 'WBS-014'"
    ).fetchall()
    assert {r["internal_task_id"] for r in rows} == {"int-1", "int-2"}


def test_duplicate_internal_task_id_violates_primary_key(conn):
    # K: internal_task_id는 PK — 중복은 IntegrityError
    _insert_task(conn, "int-1", "WBS-014")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, "int-1", "WBS-999")


def test_source_task_id_has_no_unique_index(conn):
    # 방어적 확인: source_task_id를 유일 컬럼으로 하는 UNIQUE 인덱스가 없어야 한다.
    idx_rows = conn.execute("PRAGMA index_list(wbs_task_state)").fetchall()
    for idx in idx_rows:
        if not idx["unique"]:
            continue
        cols = [r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
        assert cols != ["source_task_id"]


def test_daily_todo_item_unique_is_on_internal_task_id_not_source(conn):
    conn.execute(
        """
        INSERT INTO daily_todo_run (project_id, todo_date, status)
        VALUES ('demo', '2026-09-10', 'success')
        """
    )
    run_id = conn.execute("SELECT run_id FROM daily_todo_run").fetchone()["run_id"]

    def _item(internal_task_id, source_task_id, todo_date="2026-09-10"):
        conn.execute(
            """
            INSERT INTO daily_todo_item (
                run_id, project_id, todo_date, internal_task_id, source_task_id, category
            ) VALUES (?, 'demo', ?, ?, ?, 'OVERDUE')
            """,
            (run_id, todo_date, internal_task_id, source_task_id),
        )

    # 같은 날, 같은 source_task_id지만 internal_task_id가 다르면 둘 다 허용
    _item("int-1", "WBS-014")
    _item("int-2", "WBS-014")
    conn.commit()

    # 같은 (project_id, todo_date, internal_task_id) 조합은 중복 불가
    with pytest.raises(sqlite3.IntegrityError):
        _item("int-1", "WBS-014")


def test_daily_todo_run_unique_project_date(conn):
    conn.execute(
        "INSERT INTO daily_todo_run (project_id, todo_date, status) VALUES ('demo', '2026-09-10', 'success')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO daily_todo_run (project_id, todo_date, status) VALUES ('demo', '2026-09-10', 'failed')"
        )
