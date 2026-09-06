"""File/Folder Registry(SQLite)에 대한 공용 쓰기 헬퍼.

bootstrap.py와 incremental.py가 공유한다.
"""
from __future__ import annotations

import datetime as dt
import json


def now() -> str:
    return dt.datetime.utcnow().isoformat()


def upsert_file(conn, config, meta, in_scope: bool, status: str) -> None:
    conn.execute(
        """
        INSERT INTO files (
            project_id, drive_file_id, filename, mime_type, parent_folder_id,
            path_hint, file_category, size, modified_time, checksum, drive_version,
            processing_status, internal_content_version, in_project_scope, is_deleted,
            first_seen_at, last_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?, ?)
        ON CONFLICT(project_id, drive_file_id) DO UPDATE SET
            filename = excluded.filename,
            mime_type = excluded.mime_type,
            parent_folder_id = excluded.parent_folder_id,
            size = excluded.size,
            modified_time = excluded.modified_time,
            checksum = excluded.checksum,
            drive_version = excluded.drive_version,
            processing_status = excluded.processing_status,
            in_project_scope = excluded.in_project_scope,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        (
            config.project_id,
            meta.drive_file_id,
            meta.name,
            meta.mime_type,
            meta.parent_folder_id,
            meta.name,  # path_hint: 표기/디버깅용일 뿐 식별자로 쓰지 않음
            meta.category.value,
            meta.size,
            meta.modified_time,
            meta.md5_checksum,
            meta.head_revision_id,
            status,
            int(in_scope),
            now(),
            now(),
            now(),
        ),
    )


def log_event(conn, config, drive_file_id: str, event_type: str, details: dict) -> None:
    conn.execute(
        """
        INSERT INTO processing_events (project_id, drive_file_id, event_type, detected_at, details)
        VALUES (?, ?, ?, ?, ?)
        """,
        (config.project_id, drive_file_id, event_type, now(), json.dumps(details, ensure_ascii=False)),
    )
