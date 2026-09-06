"""Project Scope Filter.

Drive Changes Feed는 My Drive 전체 범위의 변경을 반환할 수 있으므로,
어떤 file_id가 "이 프로젝트 대상 폴더"에 속하는지를 별도로 판정해야 한다.

판정 기준은 path 문자열이 아니라 drive_file_id / parents 관계다.
Folder Registry(SQLite)를 캐시로 사용해 캐시 hit이면 API를 호출하지 않고,
캐시 miss(또는 scope_config_version 변경으로 stale)일 때만 parent chain을 조회한다.
"""
from __future__ import annotations

from typing import Optional

from .client import DriveClient
from .registry import now


class ProjectScopeFilter:
    def __init__(self, conn, config, drive_client: DriveClient):
        self._conn = conn
        self._config = config
        self._drive_client = drive_client

    def _get_cached(self, folder_id: str):
        return self._conn.execute(
            "SELECT * FROM folders WHERE project_id = ? AND folder_id = ?",
            (self._config.project_id, folder_id),
        ).fetchone()

    def _cache(self, folder_id: str, parent_folder_id: Optional[str], name: str, in_scope: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO folders (
                project_id, folder_id, parent_folder_id, folder_name,
                in_project_scope, scope_config_version, scope_resolved_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, folder_id) DO UPDATE SET
                parent_folder_id = excluded.parent_folder_id,
                folder_name = excluded.folder_name,
                in_project_scope = excluded.in_project_scope,
                scope_config_version = excluded.scope_config_version,
                scope_resolved_at = excluded.scope_resolved_at,
                updated_at = excluded.updated_at
            """,
            (
                self._config.project_id,
                folder_id,
                parent_folder_id,
                name,
                int(in_scope),
                self._config.scope_config_version,
                now(),
                now(),
            ),
        )

    def resolve_folder_scope(self, folder_id: Optional[str]) -> bool:
        if folder_id is None:
            return False

        drive_cfg = self._config.drive

        if folder_id in drive_cfg.excluded_folder_ids:
            self._cache(folder_id, None, "", False)
            return False

        if folder_id == drive_cfg.root_folder_id:
            self._cache(folder_id, None, "(root)", True)
            return True

        cached = self._get_cached(folder_id)
        if cached is not None and cached["scope_config_version"] == self._config.scope_config_version:
            return bool(cached["in_project_scope"])

        if not drive_cfg.include_subfolders:
            # 하위 폴더 자체를 scope로 인정하지 않음: root 직속 파일만 대상
            self._cache(folder_id, None, "", False)
            return False

        meta = self._drive_client.get_file_metadata(folder_id)
        if meta is None:
            self._cache(folder_id, None, "", False)
            return False

        parent_id = meta.parent_folder_id
        in_scope = self.resolve_folder_scope(parent_id) if parent_id else False
        self._cache(folder_id, parent_id, meta.name, in_scope)
        return in_scope
