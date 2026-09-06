"""Incremental Mode: 매일 자동 실행.

핵심 원칙(Architecture v3.1):
1. 전체 파일 목록을 다시 조회하지 않는다 — 저장된 last_page_token으로
   changes.list()를 호출해 "변경된 file_id"만 받는다.
2. "변경 발견"과 "내용 분석 필요 여부 판정"은 분리한다 — 이 모듈은 판정까지만 하고,
   실제 파일 다운로드나 Claude 호출은 이번 MVP 범위에 아예 존재하지 않는다.
3. Checkpoint(page token)는 이번 실행의 모든 DB 반영이 성공한 뒤에만 전진한다.
   중간에 예외가 나면 트랜잭션 전체가 롤백되어, 다음 실행이 같은 구간을 다시 받는다.
"""
from __future__ import annotations

from .client import DriveClient, raw_to_meta
from .models import FileCategory
from .registry import log_event, now, upsert_file
from .scope import ProjectScopeFilter
from engine.state.database import transaction


class BootstrapRequired(RuntimeError):
    pass


def _empty_report() -> dict:
    return {
        "changes_received": 0,
        "scope_matched": 0,
        "new": [],
        "modified": [],
        "renamed": [],
        "moved": [],
        "deleted": [],
        "out_of_scope": 0,
        "metadata_only": 0,
        "native_change_detected": [],
        "content_downloads": 0,
        "claude_api_calls": 0,
    }


def run_incremental(conn, config, drive_client: DriveClient) -> dict:
    checkpoint = conn.execute(
        "SELECT * FROM source_checkpoints WHERE project_id = ? AND source_type = 'google_drive'",
        (config.project_id,),
    ).fetchone()

    if checkpoint is None or not checkpoint["last_page_token"]:
        raise BootstrapRequired(
            f"프로젝트 '{config.project_id}'의 Drive checkpoint가 없습니다. "
            f"먼저 'python scripts/drive_bootstrap.py --project {config.project_id}'를 실행하세요."
        )

    last_page_token = checkpoint["last_page_token"]
    report = _empty_report()

    try:
        changes, new_start_page_token, _ = drive_client.list_changes(last_page_token)
        report["changes_received"] = len(changes)

        with transaction(conn):
            scope = ProjectScopeFilter(conn, config, drive_client)

            for change in changes:
                _process_change(conn, config, scope, change, report)

            conn.execute(
                """
                UPDATE source_checkpoints
                SET last_page_token = ?, pending_page_token = NULL,
                    last_successful_sync_at = ?, last_run_status = 'success', last_error = NULL,
                    updated_at = ?
                WHERE project_id = ? AND source_type = 'google_drive'
                """,
                (new_start_page_token or last_page_token, now(), now(), config.project_id),
            )

        report["result"] = "SUCCESS"
        return report

    except Exception as exc:
        # 트랜잭션은 이미 롤백된 상태 — last_page_token은 건드리지 않고 실패 상태만 별도로 기록한다.
        conn.execute(
            """
            UPDATE source_checkpoints
            SET last_run_status = 'failed', last_error = ?, updated_at = ?
            WHERE project_id = ? AND source_type = 'google_drive'
            """,
            (str(exc), now(), config.project_id),
        )
        conn.commit()
        raise


def _process_change(conn, config, scope: ProjectScopeFilter, change: dict, report: dict) -> None:
    drive_file_id = change.get("fileId")
    if not drive_file_id:
        return

    raw_file = change.get("file")
    removed = bool(change.get("removed")) or bool(raw_file and raw_file.get("trashed"))

    existing = conn.execute(
        "SELECT * FROM files WHERE project_id = ? AND drive_file_id = ?",
        (config.project_id, drive_file_id),
    ).fetchone()

    if removed:
        if existing is not None:
            conn.execute(
                """
                UPDATE files SET is_deleted = 1, processing_status = 'deleted',
                                 last_seen_at = ?, updated_at = ?
                WHERE project_id = ? AND drive_file_id = ?
                """,
                (now(), now(), config.project_id, drive_file_id),
            )
            log_event(conn, config, drive_file_id, "deleted", {"filename": existing["filename"]})
            report["deleted"].append(existing["filename"])
        return

    if raw_file is None:
        return

    meta = raw_to_meta(raw_file)

    if meta.category == FileCategory.FOLDER:
        # 폴더 자체의 변경(이름/이동)은 scope 캐시 재확인만 수행한다.
        # 주의: 이미 같은 scope_config_version으로 캐시된 폴더는 재계산되지 않으므로
        # 폴더 자체의 rename/move가 Registry의 folder_name/parent에 즉시 반영되지 않을 수 있다
        # (README/보고서에 명시한 알려진 제한사항).
        scope.resolve_folder_scope(meta.drive_file_id)
        return

    in_scope = scope.resolve_folder_scope(meta.parent_folder_id)

    if not in_scope:
        if existing is not None:
            conn.execute(
                """
                UPDATE files SET in_project_scope = 0, processing_status = 'out_of_scope',
                                 last_seen_at = ?, updated_at = ?
                WHERE project_id = ? AND drive_file_id = ?
                """,
                (now(), now(), config.project_id, drive_file_id),
            )
        else:
            upsert_file(conn, config, meta, in_scope=False, status="out_of_scope")
        log_event(conn, config, drive_file_id, "out_of_scope", {"filename": meta.name})
        report["out_of_scope"] += 1
        return

    report["scope_matched"] += 1

    if existing is None:
        upsert_file(conn, config, meta, in_scope=True, status="registered")
        log_event(conn, config, drive_file_id, "new", {"filename": meta.name})
        report["new"].append(meta.name)
        return

    reasons = []
    if existing["filename"] != meta.name:
        reasons.append("renamed")
    if existing["parent_folder_id"] != meta.parent_folder_id:
        reasons.append("moved")

    content_changed = False
    if meta.category == FileCategory.BINARY:
        content_changed = (existing["checksum"] or None) != (meta.md5_checksum or None)

    if meta.category == FileCategory.BINARY and content_changed:
        new_version = existing["internal_content_version"] + 1
        conn.execute(
            """
            UPDATE files
            SET filename = ?, parent_folder_id = ?, size = ?, modified_time = ?,
                checksum = ?, drive_version = ?, processing_status = 'modified',
                internal_content_version = ?, in_project_scope = 1,
                last_seen_at = ?, updated_at = ?
            WHERE project_id = ? AND drive_file_id = ?
            """,
            (
                meta.name, meta.parent_folder_id, meta.size, meta.modified_time,
                meta.md5_checksum, meta.head_revision_id, new_version,
                now(), now(), config.project_id, drive_file_id,
            ),
        )
        log_event(
            conn, config, drive_file_id, "modified",
            {"reasons": reasons, "internal_content_version": new_version},
        )
        report["modified"].append(meta.name)
        return

    if reasons:
        conn.execute(
            """
            UPDATE files
            SET filename = ?, parent_folder_id = ?, modified_time = ?,
                in_project_scope = 1, last_seen_at = ?, updated_at = ?
            WHERE project_id = ? AND drive_file_id = ?
            """,
            (meta.name, meta.parent_folder_id, meta.modified_time, now(), now(), config.project_id, drive_file_id),
        )
        if "renamed" in reasons:
            log_event(conn, config, drive_file_id, "renamed",
                       {"old_name": existing["filename"], "new_name": meta.name})
            report["renamed"].append(meta.name)
        if "moved" in reasons:
            log_event(conn, config, drive_file_id, "moved",
                       {"old_parent": existing["parent_folder_id"], "new_parent": meta.parent_folder_id})
            report["moved"].append(meta.name)
        return

    if meta.category == FileCategory.GOOGLE_NATIVE and existing["modified_time"] != meta.modified_time:
        conn.execute(
            "UPDATE files SET modified_time = ?, last_seen_at = ?, updated_at = ? "
            "WHERE project_id = ? AND drive_file_id = ?",
            (meta.modified_time, now(), now(), config.project_id, drive_file_id),
        )
        log_event(conn, config, drive_file_id, "native_change_detected", {"filename": meta.name})
        report["native_change_detected"].append(meta.name)
        return

    # 그 외: 실질적 변경 없음 (권한/조회시각 등 metadata만 흔들린 경우)
    conn.execute(
        "UPDATE files SET last_seen_at = ?, updated_at = ? WHERE project_id = ? AND drive_file_id = ?",
        (now(), now(), config.project_id, drive_file_id),
    )
    log_event(conn, config, drive_file_id, "metadata_only", {})
    report["metadata_only"] += 1
