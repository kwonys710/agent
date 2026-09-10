"""Google Sheets API 클라이언트 (WBS 소스 읽기 전용).

`SheetsClient`는 향후 sync 로직이 의존할 최소 인터페이스다.
`GoogleSheetsClient`가 실제 구현이고, tests/fakes.py의 `FakeSheetsClient`가 테스트용
구현이다 (둘 다 같은 메서드 시그니처만 지키면 되는 duck-typing 인터페이스).

이번 단계(Commit 2)에서는:
- 실제 네트워크 호출/인증을 수행하지 않는다.
- `GoogleSheetsClient` 생성 시 어떤 API도 호출하지 않는다 (lazy).
- 인증 배선(credential/token/scope)은 Commit 3(sync)의 책임이며 여기서 다루지 않는다.
  기존 Drive OAuth scope / token 파일은 이 모듈에서 건드리지 않는다.

메서드는 두 개뿐이다:
- get_modified_time: 스프레드시트가 "언제든 바뀌었는지"를 판단하기 위한 coarse 신호
  (Drive files.get의 modifiedTime — Sheets API 자체는 이 값을 주지 않는다).
- get_values: 시트 값 2D 배열 (Sheets API spreadsheets.values.get).
행/셀 단위 변경 감지는 API로 불가능하므로 상위 계층에서 row_hash로 계산한다.
"""
from __future__ import annotations

from typing import Optional, Protocol


class SheetsClient(Protocol):
    def get_modified_time(self, spreadsheet_id: str) -> str:
        """스프레드시트의 마지막 수정 시각(ISO8601 문자열)."""
        ...

    def get_values(self, spreadsheet_id: str, sheet_name: str) -> list[list[object]]:
        """시트 전체 값. 행 리스트이며, 각 행은 셀 리스트(뒤쪽 빈 셀은 잘려 올 수 있음)."""
        ...


class GoogleSheetsClient:
    """실제 Google API 클라이언트.

    googleapiclient는 실제로 사용될 때(=서비스 접근 시점)에만 import한다.
    생성자는 어떤 호출도 하지 않는다. 서비스 객체나 credentials는 주입해야 하며,
    주입 없이 호출하면 명확히 실패한다 (인증 배선은 Commit 3에서).
    """

    def __init__(
        self,
        *,
        sheets_service: object = None,
        drive_service: object = None,
        credentials: object = None,
    ) -> None:
        self._sheets_service = sheets_service
        self._drive_service = drive_service
        self._credentials = credentials

    def _sheets(self):
        if self._sheets_service is None:
            if self._credentials is None:
                raise RuntimeError(
                    "GoogleSheetsClient: sheets_service 또는 credentials를 주입하세요 "
                    "(인증 배선은 Commit 3에서 구현)."
                )
            from googleapiclient.discovery import build

            self._sheets_service = build("sheets", "v4", credentials=self._credentials)
        return self._sheets_service

    def _drive(self):
        if self._drive_service is None:
            if self._credentials is None:
                raise RuntimeError(
                    "GoogleSheetsClient: drive_service 또는 credentials를 주입하세요 "
                    "(인증 배선은 Commit 3에서 구현)."
                )
            from googleapiclient.discovery import build

            self._drive_service = build("drive", "v3", credentials=self._credentials)
        return self._drive_service

    def get_modified_time(self, spreadsheet_id: str) -> str:
        resp = (
            self._drive()
            .files()
            .get(fileId=spreadsheet_id, fields="modifiedTime")
            .execute()
        )
        return resp["modifiedTime"]

    def get_values(self, spreadsheet_id: str, sheet_name: str) -> list[list[object]]:
        resp = (
            self._sheets()
            .spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_name)
            .execute()
        )
        return resp.get("values", [])
