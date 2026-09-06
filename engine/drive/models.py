"""Drive 파일 메타데이터 모델과 mimeType 분류.

Architecture v3.1: Binary 파일과 Google Workspace Native 파일(Docs/Sheets/Slides)의
변경 감지 전략을 분리하기 위해 mimeType 기준으로 카테고리를 나눈다.
"""
from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Optional


class FileCategory(str, Enum):
    BINARY = "binary"
    GOOGLE_NATIVE = "google_native"
    FOLDER = "folder"
    UNSUPPORTED = "unsupported"


FOLDER_MIME = "application/vnd.google-apps.folder"

# Docs/Sheets/Slides — 이번 MVP에서 "존재는 인지하되 export/내용비교는 하지 않는" 대상
GOOGLE_NATIVE_MIME_TYPES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
}


def classify_mime_type(mime_type: str) -> FileCategory:
    if mime_type == FOLDER_MIME:
        return FileCategory.FOLDER
    if mime_type in GOOGLE_NATIVE_MIME_TYPES:
        return FileCategory.GOOGLE_NATIVE
    if mime_type.startswith("application/vnd.google-apps."):
        # forms/drawings/sites/apps-script 등 이번 MVP 범위 밖의 native 유형
        return FileCategory.UNSUPPORTED
    return FileCategory.BINARY


@dataclasses.dataclass(frozen=True)
class DriveFileMeta:
    drive_file_id: str
    name: str
    mime_type: str
    parents: list
    size: Optional[int]
    modified_time: Optional[str]
    md5_checksum: Optional[str]
    head_revision_id: Optional[str]
    trashed: bool

    @property
    def parent_folder_id(self) -> Optional[str]:
        return self.parents[0] if self.parents else None

    @property
    def category(self) -> FileCategory:
        return classify_mime_type(self.mime_type)
