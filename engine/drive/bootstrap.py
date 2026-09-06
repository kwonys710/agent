"""Bootstrap Mode: 프로젝트 최초 등록 시 1회만 실행.

- 프로젝트 root_folder_id 이하(하위 폴더 포함 여부는 config 기준)를 순회하며
  File/Folder Registry를 생성한다.
- 파일 내용은 절대 다운로드/분석하지 않는다 (metadata만 사용).
- 이미 checkpoint가 있으면 override=True 없이는 재실행을 거부한다
  (자동 스케줄이 실수로 Bootstrap을 다시 트리거하는 사고 방지).
"""
from __future__ import annotations

from .client import DriveClient
from .models import FileCategory
from .registry import log_event, now, upsert_file
from .scope import ProjectScopeFilter
from engine.state.database import transaction


class BootstrapAlreadyDone(RuntimeError):
    pass


def run_bootstrap(conn, config, drive_client: DriveClient, override: bool = False) -> dict:
    existing = conn.execute(
        "SELECT last_page_token FROM source_checkpoints WHERE project_id = ? AND source_type = 'google_drive'",
        (config.project_id,),
    ).fetchone()

    if existing is not None and existing["last_page_token"] and not override:
        raise BootstrapAlreadyDone(
            f"프로젝트 '{config.project_id}'는 이미 Bootstrap이 완료되어 있습니다 "
            f"(last_page_token={existing['last_page_token']}). "
            f"다시 실행하려면 --override를 명시적으로 지정하세요."
        )

    stats = {"files_registered": 0, "folders_visited": 0}

    with transaction(conn):
        scope = ProjectScopeFilter(conn, config, drive_client)
        _walk(drive_client, scope, conn, config, config.drive.root_folder_id, stats)

        start_page_token = drive_client.get_start_page_token()
        conn.execute(
            """
            INSERT INTO source_checkpoints (
                project_id, source_type, last_page_token, pending_page_token,
                last_successful_sync_at, last_run_status, last_error, updated_at
            )
            VALUES (?, 'google_drive', ?, NULL, ?, 'success', NULL, ?)
            ON CONFLICT(project_id, source_type) DO UPDATE SET
                last_page_token = excluded.last_page_token,
                pending_page_token = NULL,
                last_successful_sync_at = excluded.last_successful_sync_at,
                last_run_status = 'success',
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (config.project_id, start_page_token, now(), now()),
        )

    stats["start_page_token"] = start_page_token
    return stats


def _walk(drive_client, scope: ProjectScopeFilter, conn, config, folder_id: str, stats: dict) -> None:
    for meta in drive_client.list_children(folder_id):
        if meta.category == FileCategory.FOLDER:
            stats["folders_visited"] += 1
            in_scope = scope.resolve_folder_scope(meta.drive_file_id)
            if in_scope:
                _walk(drive_client, scope, conn, config, meta.drive_file_id, stats)
            continue

        in_scope = scope.resolve_folder_scope(meta.parent_folder_id)
        if not in_scope:
            continue

        upsert_file(conn, config, meta, in_scope=True, status="registered")
        log_event(conn, config, meta.drive_file_id, "new", {"filename": meta.name, "phase": "bootstrap"})
        stats["files_registered"] += 1
