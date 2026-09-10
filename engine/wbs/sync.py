"""WBS SOURCE CHANGE 경로: bootstrap + incremental sync.

- get_modified_time / get_values 같은 네트워크 read 는 트랜잭션 밖에서 수행한다.
- normalize 결과가 확정된 뒤에만 트랜잭션에 진입해 state upsert + checkpoint 를 한 번에 커밋한다.
- normalize 실패 / DB 실패 시 checkpoint(modifiedTime) 는 전진하지 않는다.
  실패 상태만 별도 커밋으로 기록한다 (Drive incremental 과 동일한 원칙).

이번 단계 범위 밖: Daily rule, Todo candidate, Daily To-Do JSON, Claude, Slack.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Callable, Optional

from engine.state.database import transaction
from .identity import KIND_AMBIGUOUS, KIND_NEW, match_task
from .normalize import NormalizedTask, normalize_sheet
from .state import (
    TaskState,
    date_to_str,
    deactivate_task_state,
    diff_fields,
    insert_task_state,
    load_states,
    update_task_state,
    utcnow_iso,
)

SOURCE_TYPE = "wbs"


class WBSNotConfigured(RuntimeError):
    """project config 에 wbs 섹션이 없음."""


class WBSBootstrapAlreadyDone(RuntimeError):
    """이미 WBS bootstrap 이 완료됨 (override 없이 재실행 거부)."""


class WBSBootstrapRequired(RuntimeError):
    """WBS checkpoint 가 없어 incremental sync 를 할 수 없음."""


def generate_internal_task_id() -> str:
    return "wbs-" + uuid.uuid4().hex


IdFactory = Callable[[], str]


# ---------------------------------------------------------------------------
# public entrypoints
# ---------------------------------------------------------------------------
def run_wbs_bootstrap(
    conn,
    config,
    client,
    *,
    id_factory: IdFactory = generate_internal_task_id,
    override: bool = False,
) -> dict:
    wcfg = _require_wbs(config)
    checkpoint = _get_checkpoint(conn, config.project_id)

    if checkpoint is not None and checkpoint["last_page_token"] and not override:
        raise WBSBootstrapAlreadyDone(
            f"프로젝트 '{config.project_id}' 의 WBS bootstrap 이 이미 완료되어 있습니다 "
            f"(modifiedTime={checkpoint['last_page_token']}). 재실행하려면 override=True."
        )

    report = _empty_report(mode="bootstrap")

    try:
        modified_time = client.get_modified_time(wcfg.spreadsheet_id)
        rows = client.get_values(wcfg.spreadsheet_id, wcfg.sheet_name)
        normalized = normalize_sheet(rows, wcfg)

        with transaction(conn):
            _apply_tasks(conn, config, wcfg, normalized.tasks, id_factory, report)
            _write_checkpoint(conn, config.project_id, modified_time)

        report["source_modified_time"] = modified_time
        report["checkpoint"] = modified_time
        report["rows_read"] = len(normalized.tasks)
        report["skipped_rows"] = list(normalized.skipped_rows)
        report["get_values_calls"] = getattr(client, "get_values_calls", None)
        report["result"] = "SUCCESS"
        return report
    except Exception as exc:  # noqa: BLE001 - checkpoint 를 전진시키지 않고 실패만 기록
        _record_failure(conn, config.project_id, exc)
        raise


def run_wbs_sync(
    conn,
    config,
    client,
    *,
    id_factory: IdFactory = generate_internal_task_id,
) -> dict:
    wcfg = _require_wbs(config)
    checkpoint = _get_checkpoint(conn, config.project_id)
    if checkpoint is None or not checkpoint["last_page_token"]:
        raise WBSBootstrapRequired(
            f"프로젝트 '{config.project_id}' 의 WBS checkpoint 가 없습니다. "
            f"먼저 run_wbs_bootstrap 을 실행하세요."
        )

    last_modified_time = checkpoint["last_page_token"]
    report = _empty_report(mode="sync")

    try:
        current_modified_time = client.get_modified_time(wcfg.spreadsheet_id)

        if current_modified_time == last_modified_time:
            # SOURCE 변경 없음 — values API 호출 0, state 변경 0.
            _touch_checkpoint_success(conn, config.project_id)
            report["unchanged_source"] = True
            report["source_modified_time"] = current_modified_time
            report["checkpoint"] = last_modified_time
            report["get_values_calls"] = getattr(client, "get_values_calls", None)
            report["result"] = "SUCCESS"
            return report

        rows = client.get_values(wcfg.spreadsheet_id, wcfg.sheet_name)
        normalized = normalize_sheet(rows, wcfg)

        with transaction(conn):
            _apply_tasks(conn, config, wcfg, normalized.tasks, id_factory, report)
            _write_checkpoint(conn, config.project_id, current_modified_time)

        report["source_modified_time"] = current_modified_time
        report["checkpoint"] = current_modified_time
        report["rows_read"] = len(normalized.tasks)
        report["skipped_rows"] = list(normalized.skipped_rows)
        report["get_values_calls"] = getattr(client, "get_values_calls", None)
        report["result"] = "SUCCESS"
        return report
    except Exception as exc:  # noqa: BLE001
        _record_failure(conn, config.project_id, exc)
        raise


# ---------------------------------------------------------------------------
# core apply (bootstrap 와 sync 공유)
# ---------------------------------------------------------------------------
def _apply_tasks(conn, config, wcfg, tasks: list, id_factory: IdFactory, report: dict) -> None:
    project_id = config.project_id
    spreadsheet_id = wcfg.spreadsheet_id
    sheet_name = wcfg.sheet_name
    now = utcnow_iso()

    active = load_states(conn, project_id, spreadsheet_id, sheet_name, active=True)
    inactive = load_states(conn, project_id, spreadsheet_id, sheet_name, active=False)
    states_by_id = {s.internal_task_id: s for s in (active + inactive)}

    claimed: set = set()
    seen: set = set()

    for task in tasks:
        result = match_task(task, active, inactive, claimed)
        parse_errors = _dedupe(task.parse_errors)
        if result.kind == KIND_AMBIGUOUS and "ambiguous_identity_match" not in parse_errors:
            parse_errors.append("ambiguous_identity_match")

        if result.internal_task_id is None:
            internal_task_id = id_factory()
            state = _build_state(
                task,
                internal_task_id=internal_task_id,
                project_id=project_id,
                wcfg=wcfg,
                parse_errors=parse_errors,
                first_seen_at=now,
                last_seen_at=now,
                last_changed_at=now,
                is_active=True,
            )
            insert_task_state(conn, state)
            seen.add(internal_task_id)
            if result.kind == KIND_AMBIGUOUS:
                report["ambiguous"].append(
                    {"internal_task_id": internal_task_id, "reason": result.ambiguous_reason}
                )
            else:
                report["new"].append(internal_task_id)
            continue

        internal_task_id = result.internal_task_id
        claimed.add(internal_task_id)
        seen.add(internal_task_id)
        old = states_by_id[internal_task_id]

        changed_fields = diff_fields(old, task)
        row_hash_changed = old.row_hash != task.row_hash
        row_key_changed = old.row_key != task.row_key
        content_changed = bool(changed_fields) or result.reactivated

        state = _build_state(
            task,
            internal_task_id=internal_task_id,
            project_id=project_id,
            wcfg=wcfg,
            parse_errors=parse_errors,
            first_seen_at=old.first_seen_at or now,
            last_seen_at=now,
            last_changed_at=now if content_changed else old.last_changed_at,
            is_active=True,
        )
        update_task_state(conn, state)

        entry = {
            "internal_task_id": internal_task_id,
            "changed_fields": changed_fields,
            "row_hash_changed": row_hash_changed,
            "row_key_changed": row_key_changed,
            "match_kind": result.kind,
        }
        if result.reactivated:
            report["reactivated"].append(entry)
        elif changed_fields:
            report["modified"].append(entry)
        else:
            report["unchanged"].append(internal_task_id)

        if result.kind == KIND_AMBIGUOUS:
            report["ambiguous"].append(
                {"internal_task_id": internal_task_id, "reason": result.ambiguous_reason}
            )

    for state in active:
        if state.internal_task_id not in seen:
            deactivate_task_state(conn, state.internal_task_id, now)
            report["deactivated"].append(state.internal_task_id)


def _build_state(
    task: NormalizedTask,
    *,
    internal_task_id: str,
    project_id: str,
    wcfg,
    parse_errors: list,
    first_seen_at: Optional[str],
    last_seen_at: Optional[str],
    last_changed_at: Optional[str],
    is_active: bool,
) -> TaskState:
    return TaskState(
        internal_task_id=internal_task_id,
        project_id=project_id,
        spreadsheet_id=wcfg.spreadsheet_id,
        sheet_name=wcfg.sheet_name,
        source_task_id=task.source_task_id,
        source_identity_key=task.source_identity_key,
        row_key=task.row_key,
        task_name=task.task_name,
        project=task.project,
        phase=task.phase,
        owner=task.owner,
        start_date=date_to_str(task.start_date),
        end_date=date_to_str(task.end_date),
        progress=task.progress,
        raw_status=task.raw_status,
        normalized_status=task.normalized_status,
        priority=task.priority,
        dependency=list(task.dependency or []),
        notes=task.notes,
        source_updated_at=task.source_updated_at,
        parse_errors=list(parse_errors),
        row_hash=task.row_hash,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        last_changed_at=last_changed_at,
        is_active=is_active,
    )


def _dedupe(values: list) -> list:
    seen: set = set()
    out: list = []
    for value in values or []:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


# ---------------------------------------------------------------------------
# checkpoint helpers (source_checkpoints, source_type='wbs')
# ---------------------------------------------------------------------------
def _get_checkpoint(conn, project_id: str):
    return conn.execute(
        "SELECT * FROM source_checkpoints WHERE project_id = ? AND source_type = ?",
        (project_id, SOURCE_TYPE),
    ).fetchone()


def _write_checkpoint(conn, project_id: str, modified_time: str) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO source_checkpoints (
            project_id, source_type, last_page_token, pending_page_token,
            last_successful_sync_at, last_run_status, last_error, updated_at
        )
        VALUES (?, ?, ?, NULL, ?, 'success', NULL, ?)
        ON CONFLICT(project_id, source_type) DO UPDATE SET
            last_page_token = excluded.last_page_token,
            pending_page_token = NULL,
            last_successful_sync_at = excluded.last_successful_sync_at,
            last_run_status = 'success',
            last_error = NULL,
            updated_at = excluded.updated_at
        """,
        (project_id, SOURCE_TYPE, modified_time, now, now),
    )


def _touch_checkpoint_success(conn, project_id: str) -> None:
    """SOURCE 무변경 실행: last_page_token 은 건드리지 않고 liveness 만 갱신."""
    now = utcnow_iso()
    conn.execute(
        """
        UPDATE source_checkpoints
        SET last_successful_sync_at = ?, last_run_status = 'success', last_error = NULL, updated_at = ?
        WHERE project_id = ? AND source_type = ?
        """,
        (now, now, project_id, SOURCE_TYPE),
    )
    conn.commit()


def _record_failure(conn, project_id: str, exc: Exception) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO source_checkpoints (
            project_id, source_type, last_page_token, pending_page_token,
            last_successful_sync_at, last_run_status, last_error, updated_at
        )
        VALUES (?, ?, NULL, NULL, NULL, 'failed', ?, ?)
        ON CONFLICT(project_id, source_type) DO UPDATE SET
            last_run_status = 'failed',
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (project_id, SOURCE_TYPE, str(exc), now),
    )
    conn.commit()


def _require_wbs(config):
    wcfg = getattr(config, "wbs", None)
    if wcfg is None:
        raise WBSNotConfigured(
            f"프로젝트 '{getattr(config, 'project_id', '?')}' 에 wbs 섹션이 없습니다."
        )
    return wcfg


def _empty_report(*, mode: str) -> dict:
    return {
        "mode": mode,
        "unchanged_source": False,
        "source_modified_time": None,
        "checkpoint": None,
        "rows_read": 0,
        "skipped_rows": [],
        "new": [],
        "modified": [],
        "unchanged": [],
        "reactivated": [],
        "deactivated": [],
        "ambiguous": [],
        "get_values_calls": None,
        "result": None,
    }


def format_report(report: dict) -> str:
    lines = [
        f"[WBS {report['mode'].title()}]",
        f"Source modified: {report.get('source_modified_time')}"
        + (" (unchanged)" if report.get("unchanged_source") else ""),
        f"Rows read: {report.get('rows_read', 0)}",
        f"New: {len(report['new'])}",
        f"Modified: {len(report['modified'])}",
        f"Unchanged: {len(report['unchanged'])}",
        f"Reactivated: {len(report['reactivated'])}",
        f"Deactivated: {len(report['deactivated'])}",
        f"Ambiguous: {len(report['ambiguous'])}",
        f"Skipped rows: {report.get('skipped_rows', [])}",
        f"Checkpoint: {report.get('checkpoint')}",
        f"Result: {report.get('result')}",
    ]
    return "\n".join(lines)
