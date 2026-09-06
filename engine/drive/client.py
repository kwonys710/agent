"""Google Drive API 클라이언트.

DriveClient는 bootstrap.py/incremental.py가 의존하는 최소 인터페이스다.
GoogleDriveClient가 실제 구현이고, tests/fakes.py의 FakeDriveClient가 테스트용 구현이다.
(둘 다 같은 메서드 시그니처만 지키면 되는 duck-typing 인터페이스)

중요: 이 클라이언트는 오직 metadata 조회(get/list)만 제공한다.
파일 content를 download하는 메서드는 이번 MVP에 존재하지 않는다 — 이것이
"Claude/코드가 파일 본문을 다운로드하지 못하게" 강제하는 장치다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol

from .models import DriveFileMeta

# Bootstrap/Incremental이 실제로 필요로 하는 필드만 요청 (전체 필드 조회 금지 = API 호출 비용 최소화)
METADATA_FIELDS = "id,name,mimeType,parents,size,modifiedTime,md5Checksum,headRevisionId,trashed"


class DriveClient(Protocol):
    def get_start_page_token(self) -> str: ...

    def list_changes(self, page_token: str):
        """returns (changes: list[dict], new_start_page_token: str, next_page_token: Optional[str])"""
        ...

    def get_file_metadata(self, file_id: str) -> Optional[DriveFileMeta]: ...

    def list_children(self, folder_id: str) -> list:
        ...


def paginate_changes(fetch_page, page_token: str):
    """changes.list의 페이지네이션 루프를 순수 함수로 분리한 것.

    fetch_page(token) -> Drive API의 changes.list 응답과 동일한 shape의 dict
    ({"changes": [...], "nextPageToken": ..., "newStartPageToken": ...})을 반환해야 한다.

    이렇게 분리해 두면 실제 googleapiclient 서비스 객체 없이도(순수 함수 테스트로)
    "여러 페이지를 전부 순회하는지", "중간 페이지의 token을 최종 checkpoint로
    잘못 쓰지 않는지"를 검증할 수 있다.
    """
    changes: list = []
    next_page_token = page_token
    new_start_page_token = None

    while True:
        resp = fetch_page(next_page_token)
        changes.extend(resp.get("changes", []))
        if "newStartPageToken" in resp:
            new_start_page_token = resp["newStartPageToken"]
        next_page_token = resp.get("nextPageToken")
        if not next_page_token:
            break

    return changes, new_start_page_token


def raw_to_meta(raw: dict) -> DriveFileMeta:
    return DriveFileMeta(
        drive_file_id=raw["id"],
        name=raw.get("name", ""),
        mime_type=raw.get("mimeType", ""),
        parents=list(raw.get("parents") or []),
        size=int(raw["size"]) if raw.get("size") is not None else None,
        modified_time=raw.get("modifiedTime"),
        md5_checksum=raw.get("md5Checksum"),
        head_revision_id=raw.get("headRevisionId"),
        trashed=bool(raw.get("trashed", False)),
    )


class GoogleDriveClient:
    """실제 Google Drive API v3 클라이언트 (OAuth Desktop App 인증).

    google-api-python-client / google-auth-oauthlib는 이 클래스가 실제로
    사용될 때(=_get_service 호출 시점)만 import한다. 그 전까지는
    이 모듈을 import해도 해당 라이브러리가 없어도 에러가 나지 않는다
    (테스트가 FakeDriveClient만 쓰고 이 클래스를 인스턴스화하지 않기 때문).
    """

    # 읽기 전용 스코프만 요청 — 최소 권한 원칙
    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

    def __init__(self, client_secret_file: Optional[str] = None, token_dir: Optional[str] = None):
        self._client_secret_file = client_secret_file or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET_FILE")
        self._token_dir = Path(token_dir or os.environ.get("GOOGLE_OAUTH_TOKEN_DIR", "./data/.tokens"))
        self._service = None

    def _authenticate(self):
        if not self._client_secret_file:
            raise RuntimeError(
                "GOOGLE_OAUTH_CLIENT_SECRET_FILE이 설정되지 않았습니다. .env를 확인하세요."
            )

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        self._token_dir.mkdir(parents=True, exist_ok=True)
        token_path = self._token_dir / "drive_token.json"

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), self.SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self._client_secret_file, self.SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")

        return creds

    def _get_service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            creds = self._authenticate()
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    def get_start_page_token(self) -> str:
        resp = self._get_service().changes().getStartPageToken().execute()
        return resp["startPageToken"]

    def list_changes(self, page_token: str):
        service = self._get_service()

        def fetch_page(token):
            return (
                service.changes()
                .list(
                    pageToken=token,
                    fields=f"nextPageToken,newStartPageToken,changes(fileId,removed,file({METADATA_FIELDS}))",
                    pageSize=100,
                )
                .execute()
            )

        changes, new_start_page_token = paginate_changes(fetch_page, page_token)
        return changes, new_start_page_token, None

    def get_file_metadata(self, file_id: str) -> Optional[DriveFileMeta]:
        try:
            raw = self._get_service().files().get(fileId=file_id, fields=METADATA_FIELDS).execute()
        except Exception:
            return None
        return raw_to_meta(raw)

    def list_children(self, folder_id: str) -> list:
        service = self._get_service()
        results: list = []
        page_token = None

        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields=f"nextPageToken,files({METADATA_FIELDS})",
                    pageToken=page_token,
                    pageSize=100,
                )
                .execute()
            )
            results.extend(raw_to_meta(f) for f in resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return results
