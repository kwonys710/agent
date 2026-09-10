"""wbs_task_state 저장소 (SQLite 전용 계층).

책임:
- active/inactive WBS task state 조회
- state insert / update / deactivate
- dependency / parse_errors JSON 직렬화
- old(TaskState) vs new(NormalizedTask) 필드 단위 diff

normalize / identity matching / orchestration 로직은 여기에 두지 않는다.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Optional

from .normalize import NormalizedTask

# state DB가 소스 현재값을 따라가기 위해 비교하는 tracked 필드.
# row_hash(= "의미 있는 스냅샷", normalize.ROW_HASH_FIELDS)와는 목적이 다르다:
# notes/phase/project/raw_status가 바뀌면 여기서는 잡히지만 row_hash는 동일할 수 있다.
TRACKED_DIFF_FIELDS = (
    "task_name",
    "owner",
    "start_date",
    "end_date",
    "progress",
    "normalized_status",
    "priority",
    "dependency",
    "notes",
    "phase",
    "project",
    "raw_status",
)

_STATE_COLUMNS = (
    "internal_task_id",
    "project_id",
    "spreadsheet_id",
    "sheet_name",
    "source_task_id",
    "source_identity_key",
    "row_key",
    "task_name",
    "project",
    "phase",
    "owner",
    "start_date",
    "end_date",
    "progress",
    "raw_status",
    "normalized_status",
    "priority",
    "dependency",
    "notes",
    "source_updated_at",
    "parse_errors",
    "row_hash",
    "first_seen_at",
    "last_seen_at",
    "last_changed_at",
    "is_active",
)


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def to_json(values: Optional[list]) -> str:
    return json.dumps(list(values or []), ensure_ascii=False)


def from_json(blob: Optional[str]) -> list:
    if not blob:
        return []
    try:
        parsed = json.loads(blob)
    except (ValueError, TypeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def date_to_str(value: object) -> Optional[str]:
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _round_progress(value: object) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 4)


@dataclasses.dataclass(frozen=True)
class TaskState:
    internal_task_id: str
    project_id: str
    spreadsheet_id: str
    sheet_name: str

    source_task_id: Optional[str]
    source_identity_key: Optional[str]
    row_key: str

    task_name: Optional[str]
    project: Optional[str]
    phase: Optional[str]
    owner: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    progress: Optional[float]
    raw_status: Optional[str]
    normalized_status: Optional[str]
    priority: Optional[str]
    dependency: list
    notes: Optional[str]
    source_updated_at: Optional[str]
    parse_errors: list

    row_hash: str
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    last_changed_at: Optional[str]
    is_active: bool


def _row_to_state(row) -> TaskState:
    return TaskState(
        internal_task_id=row["internal_task_id"],
        project_id=row["project_id"],
        spreadsheet_id=row["spreadsheet_id"],
        sheet_name=row["sheet_name"],
        source_task_id=row["source_task_id"],
        source_identity_key=row["source_identity_key"],
        row_key=row["row_key"],
        task_name=row["task_name"],
        project=row["project"],
        phase=row["phase"],
        owner=row["owner"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        progress=row["progress"],
        raw_status=row["raw_status"],
        normalized_status=row["normalized_status"],
        priority=row["priority"],
        dependency=from_json(row["dependency"]),
        notes=row["notes"],
        source_updated_at=row["source_updated_at"],
        parse_errors=from_json(row["parse_errors"]),
        row_hash=row["row_hash"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        last_changed_at=row["last_changed_at"],
        is_active=bool(row["is_active"]),
    )


def load_states(
    conn,
    project_id: str,
    spreadsheet_id: str,
    sheet_name: str,
    *,
    active: Optional[bool] = None,
) -> list:
    sql = (
        "SELECT * FROM wbs_task_state "
        "WHERE project_id = ? AND spreadsheet_id = ? AND sheet_name = ?"
    )
    params = [project_id, spreadsheet_id, sheet_name]
    if active is True:
        sql += " AND is_active = 1"
    elif active is False:
        sql += " AND is_active = 0"
    return [_row_to_state(r) for r in conn.execute(sql, params).fetchall()]


def insert_task_state(conn, state: TaskState) -> None:
    values = _state_to_params(state)
    placeholders = ", ".join("?" for _ in _STATE_COLUMNS)
    conn.execute(
        f"INSERT INTO wbs_task_state ({', '.join(_STATE_COLUMNS)}) VALUES ({placeholders})",
        values,
    )


def update_task_state(conn, state: TaskState) -> None:
    assignable = [c for c in _STATE_COLUMNS if c != "internal_task_id"]
    set_clause = ", ".join(f"{c} = ?" for c in assignable)
    params = [getattr(_ParamView(state), c) for c in assignable]
    params.append(state.internal_task_id)
    conn.execute(
        f"UPDATE wbs_task_state SET {set_clause} WHERE internal_task_id = ?",
        params,
    )


def deactivate_task_state(conn, internal_task_id: str, at: str) -> None:
    # is_active = 0 로만 내린다. last_seen_at 은 마지막으로 실제 관측된 시각을 보존한다.
    # deactivated_at 컬럼은 현재 schema에 없으므로 last_changed_at 에 시각을 남긴다.
    conn.execute(
        "UPDATE wbs_task_state SET is_active = 0, last_changed_at = ? WHERE internal_task_id = ?",
        (at, internal_task_id),
    )


class _ParamView:
    """TaskState 를 DB 파라미터(직렬화된 dependency/parse_errors, int is_active)로 본다."""

    def __init__(self, state: TaskState) -> None:
        self._state = state

    def __getattr__(self, name: str):
        state = object.__getattribute__(self, "_state")
        if name == "dependency":
            return to_json(state.dependency)
        if name == "parse_errors":
            return to_json(state.parse_errors)
        if name == "is_active":
            return 1 if state.is_active else 0
        return getattr(state, name)


def _state_to_params(state: TaskState) -> list:
    view = _ParamView(state)
    return [getattr(view, column) for column in _STATE_COLUMNS]


def diff_fields(old: TaskState, new: NormalizedTask) -> list:
    """old(state) 와 new(normalize 결과) 를 tracked 필드 단위로 비교한다."""
    new_values = {
        "task_name": new.task_name,
        "owner": new.owner,
        "start_date": date_to_str(new.start_date),
        "end_date": date_to_str(new.end_date),
        "progress": _round_progress(new.progress),
        "normalized_status": new.normalized_status,
        "priority": new.priority,
        "dependency": sorted(new.dependency or []),
        "notes": new.notes,
        "phase": new.phase,
        "project": new.project,
        "raw_status": new.raw_status,
    }
    old_values = {
        "task_name": old.task_name,
        "owner": old.owner,
        "start_date": old.start_date,
        "end_date": old.end_date,
        "progress": _round_progress(old.progress),
        "normalized_status": old.normalized_status,
        "priority": old.priority,
        "dependency": sorted(old.dependency or []),
        "notes": old.notes,
        "phase": old.phase,
        "project": old.project,
        "raw_status": old.raw_status,
    }
    return [field for field in TRACKED_DIFF_FIELDS if old_values[field] != new_values[field]]
