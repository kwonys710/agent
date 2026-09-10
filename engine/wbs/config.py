"""WBS(Google Sheets 기반) 모듈 설정.

projects/<project_id>/config.yaml의 선택적 'wbs' 섹션을 파싱/검증한다.

원칙:
- spreadsheet_id 등 프로젝트별 값은 코드에 하드코딩하지 않는다.
- 실제 시트 헤더명(한국어 등)도 코드에 하드코딩하지 않고 columns 매핑으로 받는다.
- 상태값(완료/진행/지연 등)도 하드코딩하지 않고 status_map 매핑으로 받는다.
- timezone은 stdlib zoneinfo(+ tzdata)로 검증한다 — OS 무관 동일 코드, OS별 분기 없음.

이번 단계(Commit 1)는 설정 로딩/검증까지만 담당한다.
Sheets 호출, 정규화, sync, todo rule은 이 모듈에 존재하지 않는다.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Seoul"
DEFAULT_HEADER_ROW = 1
DEFAULT_DATA_START_ROW = 2
DEFAULT_DUE_SOON_DAYS = 3
DEFAULT_PROGRESS_DONE_THRESHOLD = 1.0
DEFAULT_PROGRESS_CHANGE_MIN_DELTA = 0.1
DEFAULT_CHANGE_SIGNIFICANT_FIELDS = (
    "start_date",
    "end_date",
    "owner",
    "normalized_status",
    "progress",
)

# 매핑을 반드시 제공해야 하는 최소 논리 컬럼. task_id는 포함하지 않는다(OPTIONAL).
REQUIRED_COLUMN_KEYS = ("task_name",)

# 매핑 가능한(그러나 선택적인) 논리 컬럼 목록. 여기 없는 키도 거부하지는 않는다.
KNOWN_COLUMN_KEYS = (
    "task_id",
    "task_name",
    "project",
    "phase",
    "owner",
    "start_date",
    "end_date",
    "progress",
    "status",
    "priority",
    "dependency",
    "notes",
    "updated_at",
)

PLACEHOLDER_VALUES = {"", "CHANGE_ME", None}


class WBSConfigError(RuntimeError):
    """wbs 섹션이 없거나(존재하는데) 잘못되었거나, 필수 값이 비어 있는 경우."""


def _validate_timezone(tz_value: object) -> str:
    if not isinstance(tz_value, str) or not tz_value.strip():
        raise WBSConfigError("wbs.timezone은 비어 있지 않은 문자열이어야 합니다.")
    tz_value = tz_value.strip()
    try:
        ZoneInfo(tz_value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WBSConfigError(
            f"wbs.timezone이 유효한 IANA timezone이 아닙니다: {tz_value!r} ({exc})"
        ) from exc
    return tz_value


def _resolve_spreadsheet_id(project_id: Optional[str], raw_value: object) -> str:
    if raw_value not in PLACEHOLDER_VALUES:
        return str(raw_value)

    if project_id:
        env_key = f"PM_{project_id.upper()}_WBS_SPREADSHEET_ID"
        env_value = os.environ.get(env_key)
        if env_value:
            return env_value

    raise WBSConfigError(
        "wbs.spreadsheet_id가 설정되지 않았습니다. "
        "config.yaml의 wbs.spreadsheet_id를 채우거나 "
        "환경변수 PM_<PROJECT_ID 대문자>_WBS_SPREADSHEET_ID를 설정하세요."
    )


def _coerce_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WBSConfigError(f"wbs.{key}는 정수여야 합니다: {value!r}") from exc


def _coerce_float(raw: dict, key: str, default: float) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise WBSConfigError(f"wbs.{key}는 숫자여야 합니다: {value!r}") from exc


@dataclasses.dataclass(frozen=True)
class WBSConfig:
    """WBS 소스 1개(스프레드시트 1개 + 시트 1개)에 대한 설정.

    columns: 논리 필드명 -> 실제 시트 헤더명(또는 열 문자). 미지정 필드는 해당 규칙 비활성.
    status_map: normalized_status -> 시트 원문 상태값 리스트.
    """

    spreadsheet_id: str
    sheet_name: str
    timezone: str = DEFAULT_TIMEZONE
    header_row: int = DEFAULT_HEADER_ROW
    data_start_row: int = DEFAULT_DATA_START_ROW
    due_soon_days: int = DEFAULT_DUE_SOON_DAYS
    progress_done_threshold: float = DEFAULT_PROGRESS_DONE_THRESHOLD
    progress_change_min_delta: float = DEFAULT_PROGRESS_CHANGE_MIN_DELTA
    columns: dict = dataclasses.field(default_factory=dict)
    status_map: dict = dataclasses.field(default_factory=dict)
    blocked_keywords: list = dataclasses.field(default_factory=list)
    change_significant_fields: list = dataclasses.field(
        default_factory=lambda: list(DEFAULT_CHANGE_SIGNIFICANT_FIELDS)
    )

    @classmethod
    def from_raw(cls, raw: object, *, project_id: Optional[str] = None) -> "WBSConfig":
        if not isinstance(raw, dict):
            raise WBSConfigError("config.yaml의 'wbs' 섹션은 매핑(dict)이어야 합니다.")

        spreadsheet_id = _resolve_spreadsheet_id(project_id, raw.get("spreadsheet_id"))

        sheet_name = raw.get("sheet_name")
        if not isinstance(sheet_name, str) or not sheet_name.strip():
            raise WBSConfigError("wbs.sheet_name이 설정되지 않았습니다.")
        sheet_name = sheet_name.strip()

        timezone = _validate_timezone(raw.get("timezone", DEFAULT_TIMEZONE))

        columns_raw = raw.get("columns") or {}
        if not isinstance(columns_raw, dict):
            raise WBSConfigError("wbs.columns는 매핑(dict)이어야 합니다.")
        columns = {
            str(key): str(value)
            for key, value in columns_raw.items()
            if value not in PLACEHOLDER_VALUES
        }

        missing = [key for key in REQUIRED_COLUMN_KEYS if key not in columns]
        if missing:
            raise WBSConfigError(
                "wbs.columns에 필수 논리 컬럼 매핑이 없습니다: " + ", ".join(missing)
            )

        status_map_raw = raw.get("status_map") or {}
        if not isinstance(status_map_raw, dict):
            raise WBSConfigError("wbs.status_map은 매핑(dict)이어야 합니다.")
        status_map = {
            str(normalized): [str(v) for v in (values or [])]
            for normalized, values in status_map_raw.items()
        }

        blocked_keywords = [str(v) for v in (raw.get("blocked_keywords") or [])]

        change_significant_fields = raw.get("change_significant_fields")
        if change_significant_fields is None:
            change_significant_fields = list(DEFAULT_CHANGE_SIGNIFICANT_FIELDS)
        else:
            change_significant_fields = [str(v) for v in change_significant_fields]

        return cls(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            timezone=timezone,
            header_row=_coerce_int(raw, "header_row", DEFAULT_HEADER_ROW),
            data_start_row=_coerce_int(raw, "data_start_row", DEFAULT_DATA_START_ROW),
            due_soon_days=_coerce_int(raw, "due_soon_days", DEFAULT_DUE_SOON_DAYS),
            progress_done_threshold=_coerce_float(
                raw, "progress_done_threshold", DEFAULT_PROGRESS_DONE_THRESHOLD
            ),
            progress_change_min_delta=_coerce_float(
                raw, "progress_change_min_delta", DEFAULT_PROGRESS_CHANGE_MIN_DELTA
            ),
            columns=columns,
            status_map=status_map,
            blocked_keywords=blocked_keywords,
            change_significant_fields=change_significant_fields,
        )

    def zoneinfo(self) -> ZoneInfo:
        """검증된 timezone에 대한 ZoneInfo. (향후 timezone-aware rule helper가 사용)"""
        return ZoneInfo(self.timezone)
