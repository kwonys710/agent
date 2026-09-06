"""폴더가 프로젝트 scope 경계를 넘나들 때(내부↔외부 이동)의 처리.

원칙:
- 내부 → 외부: 이미 Registry에 알려진 하위 폴더/파일만 대상으로 scope 상태를 갱신한다.
  Drive API를 호출하지 않는다 (우리가 이미 알고 있는 것만 갱신).
- 외부 → 내부: 새로 scope에 들어온 그 폴더 하위만 Drive API로 나열(files().list, parent 한정)한다.
  프로젝트 전체나 My Drive 전체를 다시 나열하지 않는다.
- 어느 경우에도 파일 content 다운로드나 Claude 호출은 발생하지 않는다.
"""
from __future__ import annotations

from .models import FileCategory
from .registry import log_event, now, upsert_file


def cascade_mark_out_of_scope(conn, config, folder_id: str, report: dict) -> None:
    """folder_id 하위(Registry에 이미 등록된 것만)를 재귀적으로 out_of_scope 처리한다."""
    child_folders = conn.execute(
        "SELECT folder_id FROM folders WHERE project_id = ? AND parent_folder_id = ? AND in_project_scope = 1",
        (config.project_id, folder_id),
    ).fetchall()
    for row in child_folders:
        conn.execute(
            """
            UPDATE folders
            SET in_project_scope = 0, scope_config_version = ?, scope_resolved_at = ?, updated_at = ?
            WHERE project_id = ? AND folder_id = ?
            """,
            (config.scope_config_version, now(), now(), config.project_id, row["folder_id"]),
        )
        cascade_mark_out_of_scope(conn, config, row["folder_id"], report)

    child_files = conn.execute(
        "SELECT drive_file_id, filename FROM files WHERE project_id = ? AND parent_folder_id = ? AND in_project_scope = 1",
        (config.project_id, folder_id),
    ).fetchall()
    for row in child_files:
        conn.execute(
            """
            UPDATE files
            SET in_project_scope = 0, processing_status = 'out_of_scope', last_seen_at = ?, updated_at = ?
            WHERE project_id = ? AND drive_file_id = ?
            """,
            (now(), now(), config.project_id, row["drive_file_id"]),
        )
        log_event(
            conn, config, row["drive_file_id"], "out_of_scope",
            {"filename": row["filename"], "reason": "ancestor_folder_moved_out_of_scope"},
        )
        report["out_of_scope"] += 1


def bring_subtree_into_scope(conn, config, drive_client, scope, folder_id: str, report: dict) -> None:
    """folder_id 하위만 Drive API로 나열해 새로 등록/scope 갱신한다 (프로젝트 전체 재스캔 금지)."""
    for meta in drive_client.list_children(folder_id):
        if meta.category == FileCategory.FOLDER:
            scope.set_scope(meta.drive_file_id, folder_id, meta.name, True)
            log_event(conn, config, meta.drive_file_id, "new", {"filename": meta.name, "kind": "folder"})
            bring_subtree_into_scope(conn, config, drive_client, scope, meta.drive_file_id, report)
            continue

        existing = conn.execute(
            "SELECT * FROM files WHERE project_id = ? AND drive_file_id = ?",
            (config.project_id, meta.drive_file_id),
        ).fetchone()

        if existing is None:
            upsert_file(conn, config, meta, in_scope=True, status="registered")
            log_event(conn, config, meta.drive_file_id, "new", {"filename": meta.name})
            report["new"].append(meta.name)
        else:
            conn.execute(
                """
                UPDATE files
                SET in_project_scope = 1, processing_status = 'registered',
                    parent_folder_id = ?, last_seen_at = ?, updated_at = ?
                WHERE project_id = ? AND drive_file_id = ?
                """,
                (folder_id, now(), now(), config.project_id, meta.drive_file_id),
            )
            log_event(
                conn, config, meta.drive_file_id, "new",
                {"filename": meta.name, "reason": "folder_moved_into_scope"},
            )
            report["new"].append(meta.name)
