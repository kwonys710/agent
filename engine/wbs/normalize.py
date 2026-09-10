"""WBS raw 시트 → NormalizedTask 정규화.

원칙:
- 시트 헤더명/상태 문자열은 코드에 하드코딩하지 않는다 (WBSConfig.columns / status_map).
- 파싱 실패는 행을 버리지 않고 parse_errors에 기록한다.
- internal_task_id는 여기서 만들지 않는다 (항상 None). 영구 ID 발급/기존 state 매칭은
  Commit 3(sync)의 책임이다.
- source_identity_key는 "매칭 후보 키"일 뿐 PK가 아니고 유일성도 보장하지 않는다.
- row_hash는 "의미 있는 내용 변경" 판정용 스냅샷 해시이며, 행 위치(row_key)와 무관하다.

이 모듈은 순수 함수다 — 네트워크/DB/파일시스템 접근이 없다.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
from enum import Enum
from typing import Optional

from .config import WBSConfig

# --- 상수 ---------------------------------------------------------------------

_MISSING = "__MISSING__"  # source_identity_key/row_hash 직렬화 시 누락 필드 sentinel

# source_identity_key 입력: 상대적으로 덜 자주 바뀌는 필드만. (PK 아님 / 충돌 가능)
IDENTITY_FIELDS = ("task_name", "phase", "start_date")

# row_hash 입력: state 동기화에 필요한 의미 필드. notes/row_key/위치정보는 제외.
ROW_HASH_FIELDS = (
    "task_name",
    "owner",
    "start_date",
    "end_date",
    "progress",
    "normalized_status",
    "priority",
    "dependency",
)

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")


class NormalizedStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    ON_HOLD = "ON_HOLD"
    UNKNOWN = "UNKNOWN"


class WBSNormalizeError(RuntimeError):
    """시트 구조가 config와 맞지 않는 경우(예: 매핑된 헤더명이 시트에 없음)."""


@dataclasses.dataclass(frozen=True)
class NormalizedTask:
    source_task_id: Optional[str]
    source_identity_key: str
    row_key: str

    task_name: Optional[str]
    project: Optional[str]
    phase: Optional[str]
    owner: Optional[str]

    start_date: Optional[dt.date]
    end_date: Optional[dt.date]
    progress: Optional[float]

    raw_status: Optional[str]
    normalized_status: str
    priority: Optional[str]

    dependency: list
    notes: Optional[str]

    source_updated_at: Optional[str]
    row_hash: str
    parse_errors: list

    # normalize 단계에서는 영구 ID를 만들지 않는다.
    internal_task_id: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class NormalizationResult:
    tasks: list
    skipped_rows: list  # 완전히 빈 행의 시트 행 번호


# --- 셀/값 파서 --------------------------------------------------------------


def _clean_cell(value: object) -> object:
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return value


def _as_text(value: object) -> Optional[str]:
    cleaned = _clean_cell(value)
    if cleaned is None:
        return None
    return cleaned if isinstance(cleaned, str) else str(cleaned)


def parse_date(value: object) -> tuple[Optional[dt.date], bool]:
    """(date|None, ok). 빈 값은 (None, True). 파싱 불가는 (None, False)."""
    if value is None:
        return None, True
    if isinstance(value, dt.datetime):
        return value.date(), True
    if isinstance(value, dt.date):
        return value, True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, True
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(text, fmt).date(), True
            except ValueError:
                continue
        return None, False
    # 숫자 serial date 등은 이번 단계에서 지원하지 않는다 (실데이터 확인 후 확장).
    return None, False


def parse_progress(value: object) -> tuple[Optional[float], bool]:
    """(0.0~1.0|None, ok).

    - 빈 값 → (None, True)
    - 0~1 → fraction 그대로
    - 1 초과 ~ 100 → percent 로 해석해 /100
    - 100 초과 / 음수 → invalid
    - '50%' → 0.5, '100%' → 1.0
    """
    if value is None:
        return None, True
    if isinstance(value, bool):
        return None, False

    is_percent = False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, True
        if text.endswith("%"):
            is_percent = True
            text = text[:-1].strip()
        try:
            number = float(text)
        except ValueError:
            return None, False
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None, False

    if is_percent:
        if 0.0 <= number <= 100.0:
            return number / 100.0, True
        return None, False

    if 0.0 <= number <= 1.0:
        return number, True
    if 1.0 < number <= 100.0:
        return number / 100.0, True
    return None, False


def normalize_status(raw_status: Optional[str], status_map: dict) -> str:
    """status_map(normalized -> [raw values])만으로 판정. 실패 시 UNKNOWN."""
    if raw_status is None:
        return NormalizedStatus.UNKNOWN.value
    candidate = raw_status.strip()
    if not candidate:
        return NormalizedStatus.UNKNOWN.value
    folded = candidate.casefold()
    for normalized, raw_values in (status_map or {}).items():
        for raw_value in raw_values or []:
            rv = str(raw_value).strip()
            if candidate == rv or folded == rv.casefold():
                return str(normalized)
    return NormalizedStatus.UNKNOWN.value


def parse_dependency(value: object) -> list:
    """쉼표/세미콜론 기준 분리 후 trim. 구조화가 애매하면 원문 1개 항목으로 유지."""
    if value is None:
        return []
    if not isinstance(value, str):
        text = str(value).strip()
        return [text] if text else []
    parts = [part.strip() for part in re.split(r"[,;]", value)]
    return [part for part in parts if part]


# --- identity / hash --------------------------------------------------------


def _serialize_component(value: object) -> object:
    if value is None:
        return _MISSING
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else _MISSING
    if isinstance(value, (list, tuple)):
        return [_serialize_component(item) for item in value]
    return value


def compute_source_identity_key(
    task_name: Optional[str],
    phase: Optional[str],
    start_date: Optional[dt.date],
) -> str:
    """매칭 후보 키 (candidate matching key only).

    PK 아님. 유일성 보장 안 함. 충돌 가능. Commit 3에서 ambiguous match를 처리해야 한다.
    """
    payload = {
        "task_name": _serialize_component(task_name),
        "phase": _serialize_component(phase),
        "start_date": _serialize_component(start_date),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_row_hash(fields: dict) -> str:
    payload = {}
    for key in ROW_HASH_FIELDS:
        value = fields.get(key)
        if key == "dependency":
            value = sorted(value or [])
        payload[key] = _serialize_component(value)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- 시트 정규화 ------------------------------------------------------------


def _resolve_column_indexes(header_cells: list, config: WBSConfig) -> dict:
    header_index: dict = {}
    for idx, cell in enumerate(header_cells):
        name = _as_text(cell)
        if name is not None and name not in header_index:
            header_index[name] = idx

    resolved: dict = {}
    missing: list = []
    for logical_field, header_name in config.columns.items():
        key = header_name.strip()
        if key in header_index:
            resolved[logical_field] = header_index[key]
        else:
            missing.append(f"{logical_field}='{header_name}'")

    if missing:
        raise WBSNormalizeError(
            "시트 헤더에서 매핑된 컬럼을 찾지 못했습니다: " + ", ".join(sorted(missing))
        )
    return resolved


def _cell(row: list, idx: Optional[int]) -> object:
    if idx is None or idx >= len(row):
        return None
    return _clean_cell(row[idx])


def normalize_sheet(rows: list, config: WBSConfig) -> NormalizationResult:
    """시트 2D 값 → NormalizedTask 목록.

    rows[0] = 시트 1행. header_row / data_start_row 는 1-indexed.
    row_key 는 "{sheet_name}!R{시트행번호}".
    """
    header_idx = config.header_row - 1
    if header_idx < 0 or header_idx >= len(rows):
        raise WBSNormalizeError(
            f"header_row={config.header_row} 위치에 헤더 행이 없습니다 (행 수={len(rows)})."
        )

    column_indexes = _resolve_column_indexes(rows[header_idx], config)

    tasks: list = []
    skipped_rows: list = []

    for offset, row in enumerate(rows[config.data_start_row - 1 :]):
        sheet_row_number = config.data_start_row + offset
        row = row or []

        if all(_clean_cell(cell) is None for cell in row):
            skipped_rows.append(sheet_row_number)
            continue

        task = _normalize_row(row, sheet_row_number, column_indexes, config)
        tasks.append(task)

    return NormalizationResult(tasks=tasks, skipped_rows=skipped_rows)


def _normalize_row(
    row: list,
    sheet_row_number: int,
    column_indexes: dict,
    config: WBSConfig,
) -> NormalizedTask:
    parse_errors: list = []

    def text_field(name: str) -> Optional[str]:
        return _as_text(_cell(row, column_indexes.get(name)))

    task_name = text_field("task_name")
    if task_name is None:
        parse_errors.append("missing_task_name")

    start_date, start_ok = parse_date(_cell(row, column_indexes.get("start_date")))
    if not start_ok:
        parse_errors.append("invalid_start_date")

    end_date, end_ok = parse_date(_cell(row, column_indexes.get("end_date")))
    if not end_ok:
        parse_errors.append("invalid_end_date")

    progress, progress_ok = parse_progress(_cell(row, column_indexes.get("progress")))
    if not progress_ok:
        parse_errors.append("invalid_progress")

    raw_status = text_field("status")
    normalized_status = normalize_status(raw_status, config.status_map)

    dependency = parse_dependency(_cell(row, column_indexes.get("dependency")))

    task_name_v = task_name
    project = text_field("project")
    phase = text_field("phase")
    owner = text_field("owner")
    priority = text_field("priority")
    notes = text_field("notes")
    source_task_id = text_field("task_id")
    source_updated_at = text_field("updated_at")

    row_hash = compute_row_hash(
        {
            "task_name": task_name_v,
            "owner": owner,
            "start_date": start_date,
            "end_date": end_date,
            "progress": progress,
            "normalized_status": normalized_status,
            "priority": priority,
            "dependency": dependency,
        }
    )

    return NormalizedTask(
        source_task_id=source_task_id,
        source_identity_key=compute_source_identity_key(task_name_v, phase, start_date),
        row_key=f"{config.sheet_name}!R{sheet_row_number}",
        task_name=task_name_v,
        project=project,
        phase=phase,
        owner=owner,
        start_date=start_date,
        end_date=end_date,
        progress=progress,
        raw_status=raw_status,
        normalized_status=normalized_status,
        priority=priority,
        dependency=dependency,
        notes=notes,
        source_updated_at=source_updated_at,
        row_hash=row_hash,
        parse_errors=parse_errors,
        internal_task_id=None,
    )
