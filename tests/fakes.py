"""테스트 전용 가짜 Drive 클라이언트.

실제 Google API를 호출하지 않고, 메모리 상의 파일/폴더 상태와
"변경 로그"를 흉내 내어 changes.list(pageToken)의 증분 동작을 재현한다.
"""
from __future__ import annotations

from engine.drive.client import paginate_changes, raw_to_meta


class FakeDriveClient:
    def __init__(self, page_size: int = 100) -> None:
        self._files: dict = {}
        self._change_log: list = []  # [(token:int, change_dict), ...]
        self._token_counter = 0
        # 작게 설정하면 list_changes가 실제 Drive API처럼 여러 페이지로 나뉘어 반환된다.
        self.page_size = page_size

    # ------------------------------------------------------------------
    # 테스트에서 Drive 상태를 조작하기 위한 헬퍼
    # ------------------------------------------------------------------
    def _bump_token(self) -> int:
        self._token_counter += 1
        return self._token_counter

    def _record_change(self, file_id: str, raw: dict) -> None:
        token = self._bump_token()
        self._change_log.append((token, {"fileId": file_id, "removed": False, "file": dict(raw)}))

    def add_file(self, file_id, name, mime_type, parent, size=100, checksum="h0", modified="t0"):
        raw = {
            "id": file_id, "name": name, "mimeType": mime_type,
            "parents": [parent] if parent else [],
            "size": size, "modifiedTime": modified,
            "md5Checksum": checksum, "headRevisionId": "r1", "trashed": False,
        }
        self._files[file_id] = raw
        self._record_change(file_id, raw)
        return raw

    def add_folder(self, folder_id, name, parent):
        raw = {
            "id": folder_id, "name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent] if parent else [],
            "size": None, "modifiedTime": "t0",
            "md5Checksum": None, "headRevisionId": None, "trashed": False,
        }
        self._files[folder_id] = raw
        self._record_change(folder_id, raw)
        return raw

    def modify_file(self, file_id, **kwargs):
        raw = self._files[file_id]
        raw.update(kwargs)
        self._record_change(file_id, raw)

    def trash_file(self, file_id):
        raw = self._files[file_id]
        raw["trashed"] = True
        self._record_change(file_id, raw)

    # ------------------------------------------------------------------
    # DriveClient 인터페이스 구현 (content download 메서드는 존재하지 않음)
    # ------------------------------------------------------------------
    def get_start_page_token(self) -> str:
        return str(self._token_counter)

    def get_file_metadata(self, file_id):
        raw = self._files.get(file_id)
        if raw is None:
            return None
        return raw_to_meta(raw)

    def list_children(self, folder_id):
        return [
            raw_to_meta(raw)
            for raw in self._files.values()
            if not raw.get("trashed") and (raw.get("parents") or [None])[0] == folder_id
        ]

    def list_changes(self, page_token: str):
        """실제 GoogleDriveClient.list_changes와 동일한 paginate_changes 루프를 사용해
        여러 페이지로 나뉜 changes.list 응답을 흉내 낸다."""
        start = int(page_token)
        pending = [change for token, change in self._change_log if token > start]
        page_size = self.page_size
        final_token = str(self._token_counter)

        def fetch_page(offset_token):
            offset = int(offset_token)
            page_items = pending[offset: offset + page_size]
            next_offset = offset + page_size
            resp: dict = {"changes": page_items}
            if next_offset < len(pending):
                resp["nextPageToken"] = str(next_offset)
            else:
                resp["newStartPageToken"] = final_token
            return resp

        changes, new_start_page_token = paginate_changes(fetch_page, "0")
        return changes, new_start_page_token, None
