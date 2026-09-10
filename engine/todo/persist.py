"""daily_todo_run / daily_todo_item 저장소 (SQLite 전용).

- 같은 날짜 재실행은 UNIQUE 위반이 아니라 upsert.
- daily_todo_item 은 "해당 실행일의 최신 snapshot" — 이번 selected 에 없는 오늘 item 은 삭제.
- 기존 오늘 item 의 resolved / created_at 은 재실행이 덮어쓰지 않는다.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Optional

from engine.state.database import transaction

# FOLLOW_UP 기준일로 인정하는 이전 run status (failed 는 제외).
_PREVIOUS_RUN_STATUSES = ("success", "needs_review")


@dataclasses.dataclass(frozen=True)
class PreviousTodoSnapshot:
    todo_date: Optional[str]   # 오늘보다 이전인 가장 최근 Daily To-Do 실행일 (없으면 None)
    items: list                # 그 실행일의 resolved == 0 인 daily_todo_item 들


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def get_daily_run(conn, project_id: str, todo_date: str):
    return conn.execute(
        "SELECT * FROM daily_todo_run WHERE project_id = ? AND todo_date = ?",
        (project_id, todo_date),
    ).fetchone()


def get_daily_items(conn, project_id: str, todo_date: str) -> list:
    rows = conn.execute(
        "SELECT * FROM daily_todo_item WHERE project_id = ? AND todo_date = ? ORDER BY item_id",
        (project_id, todo_date),
    ).fetchall()
    return [dict(row) for row in rows]


def get_previous_unresolved_items(conn, project_id: str, previous_date: str) -> list:
    rows = conn.execute(
        """
        SELECT internal_task_id, source_task_id, category, resolved
        FROM daily_todo_item
        WHERE project_id = ? AND todo_date = ? AND resolved = 0
        """,
        (project_id, previous_date),
    ).fetchall()
    return [dict(row) for row in rows]


def get_previous_unresolved_snapshot(conn, project_id: str, before_date: str) -> PreviousTodoSnapshot:
    """FOLLOW_UP 기준: 오늘보다 이전인 가장 최근 Daily To-Do 실행일의 unresolved item 들.

    정확한 전일을 강제하지 않는다 (주말/공휴일/스케줄러 미실행 고려).
    failed run 은 기준일에서 제외한다.
    """
    placeholders = ",".join("?" for _ in _PREVIOUS_RUN_STATUSES)
    row = conn.execute(
        f"""
        SELECT MAX(todo_date) AS todo_date
        FROM daily_todo_run
        WHERE project_id = ? AND todo_date < ? AND status IN ({placeholders})
        """,
        (project_id, before_date, *_PREVIOUS_RUN_STATUSES),
    ).fetchone()

    previous_date = row["todo_date"] if row else None
    if not previous_date:
        return PreviousTodoSnapshot(None, [])

    return PreviousTodoSnapshot(
        previous_date,
        get_previous_unresolved_items(conn, project_id, previous_date),
    )


def save_daily_snapshot(
    conn,
    *,
    project_id: str,
    todo_date: str,
    run_fields: dict,
    items: list,
) -> int:
    """daily_todo_run upsert + daily_todo_item snapshot 을 단일 트랜잭션으로 반영. run_id 반환."""
    now = _utcnow_iso()

    with transaction(conn):
        existing_run = get_daily_run(conn, project_id, todo_date)
        created_at = existing_run["created_at"] if existing_run else now

        conn.execute(
            """
            INSERT INTO daily_todo_run (
                project_id, todo_date, status, source_synced, candidate_count,
                claude_used, claude_task_count, guardrail_hit, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, todo_date) DO UPDATE SET
                status = excluded.status,
                source_synced = excluded.source_synced,
                candidate_count = excluded.candidate_count,
                claude_used = excluded.claude_used,
                claude_task_count = excluded.claude_task_count,
                guardrail_hit = excluded.guardrail_hit
            """,
            (
                project_id,
                todo_date,
                run_fields.get("status"),
                int(bool(run_fields.get("source_synced"))),
                int(run_fields.get("candidate_count", 0)),
                int(bool(run_fields.get("claude_used"))),
                int(run_fields.get("claude_task_count", 0)),
                run_fields.get("guardrail_hit"),
                created_at,
            ),
        )

        run_id = conn.execute(
            "SELECT run_id FROM daily_todo_run WHERE project_id = ? AND todo_date = ?",
            (project_id, todo_date),
        ).fetchone()["run_id"]

        selected_ids = []
        for item in items:
            internal_task_id = item["internal_task_id"]
            selected_ids.append(internal_task_id)

            existing_item = conn.execute(
                """
                SELECT resolved, created_at FROM daily_todo_item
                WHERE project_id = ? AND todo_date = ? AND internal_task_id = ?
                """,
                (project_id, todo_date, internal_task_id),
            ).fetchone()

            item_created_at = existing_item["created_at"] if existing_item else now

            conn.execute(
                """
                INSERT INTO daily_todo_item (
                    run_id, project_id, todo_date, internal_task_id, source_task_id, row_key,
                    category, also_categories, title, owner, due_date, status, priority, reason,
                    needs_confirmation, resolved, source_ref, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(project_id, todo_date, internal_task_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    source_task_id = excluded.source_task_id,
                    row_key = excluded.row_key,
                    category = excluded.category,
                    also_categories = excluded.also_categories,
                    title = excluded.title,
                    owner = excluded.owner,
                    due_date = excluded.due_date,
                    status = excluded.status,
                    priority = excluded.priority,
                    reason = excluded.reason,
                    needs_confirmation = excluded.needs_confirmation,
                    source_ref = excluded.source_ref
                    -- resolved / created_at 는 의도적으로 보존 (재실행이 되돌리지 않음)
                """,
                (
                    run_id,
                    project_id,
                    todo_date,
                    internal_task_id,
                    item.get("source_task_id"),
                    item.get("row_key"),
                    item["category"],
                    _json(item.get("also_categories")),
                    item.get("title"),
                    item.get("owner"),
                    item.get("due_date"),
                    item.get("status"),
                    item.get("priority"),
                    item.get("reason"),
                    int(bool(item.get("needs_confirmation"))),
                    _json(item.get("source_ref")),
                    item_created_at,
                ),
            )

        # 이번 snapshot 에 없는 오늘 item 은 stale — 삭제 (해당 실행일 최신 상태만 유지).
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            conn.execute(
                f"""
                DELETE FROM daily_todo_item
                WHERE project_id = ? AND todo_date = ?
                  AND internal_task_id NOT IN ({placeholders})
                """,
                (project_id, todo_date, *selected_ids),
            )
        else:
            conn.execute(
                "DELETE FROM daily_todo_item WHERE project_id = ? AND todo_date = ?",
                (project_id, todo_date),
            )

    return run_id


def _json(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else [], ensure_ascii=False)
