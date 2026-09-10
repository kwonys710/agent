"""WBS 정규화 테스트 (Commit 2).

Google API 실제 호출 없음. NormalizedTask + parsing/hash 규칙 + FakeSheetsClient만 검증.
"""
import datetime as dt

import pytest

from engine.wbs.config import WBSConfig
from engine.wbs.normalize import (
    NormalizationResult,
    NormalizedStatus,
    NormalizedTask,
    WBSNormalizeError,
    compute_row_hash,
    compute_source_identity_key,
    normalize_sheet,
    normalize_status,
    parse_date,
    parse_dependency,
    parse_progress,
)
from tests.fakes import FakeSheetsClient

HEADER = ["ID", "Task", "Phase", "Owner", "Start", "End", "Progress", "Status", "Priority", "Deps", "Notes"]


def make_config(**overrides):
    raw = {
        "spreadsheet_id": "SHEET",
        "sheet_name": "WBS",
        "columns": {
            "task_id": "ID",
            "task_name": "Task",
            "phase": "Phase",
            "owner": "Owner",
            "start_date": "Start",
            "end_date": "End",
            "progress": "Progress",
            "status": "Status",
            "priority": "Priority",
            "dependency": "Deps",
            "notes": "Notes",
        },
        "status_map": {
            "DONE": ["완료", "Done"],
            "IN_PROGRESS": ["진행", "In Progress"],
            "OPEN": ["대기"],
            "BLOCKED": ["지연"],
        },
    }
    raw.update(overrides)
    return WBSConfig.from_raw(raw)


def row(**cells):
    order = ["id", "task", "phase", "owner", "start", "end", "progress", "status", "priority", "deps", "notes"]
    return [cells.get(key, "") for key in order]


def one_task(cfg=None, **cells):
    cfg = cfg or make_config()
    result = normalize_sheet([HEADER, row(**cells)], cfg)
    assert len(result.tasks) == 1
    return result.tasks[0]


# --- 1~6: 기본 행 처리 -------------------------------------------------------
def test_minimal_row():
    task = one_task(task="설계 문서 작성")
    assert isinstance(task, NormalizedTask)
    assert task.task_name == "설계 문서 작성"
    assert task.internal_task_id is None
    assert task.parse_errors == []
    assert task.row_key == "WBS!R2"


def test_optional_task_id_present():
    task = one_task(id="WBS-014", task="색인 정책")
    assert task.source_task_id == "WBS-014"


def test_task_id_column_absent_is_fine():
    cfg = make_config(columns={"task_name": "Task", "owner": "Owner"})
    result = normalize_sheet([["Task", "Owner"], ["작업 A", "김"]], cfg)
    assert result.tasks[0].source_task_id is None
    assert result.tasks[0].owner == "김"


def test_owner_absent():
    task = one_task(task="작업", owner="")
    assert task.owner is None


def test_blank_row_skipped():
    cfg = make_config()
    rows = [HEADER, row(task="A"), ["", "", "", ""], row(task="B")]
    result = normalize_sheet(rows, cfg)
    assert [t.task_name for t in result.tasks] == ["A", "B"]
    assert result.skipped_rows == [3]  # HEADER=R1, A=R2, blank=R3, B=R4


def test_missing_task_name_value_keeps_row_with_parse_error():
    task = one_task(task="", owner="담당자있음")
    assert task.task_name is None
    assert "missing_task_name" in task.parse_errors
    assert task.owner == "담당자있음"


# --- 7: 날짜 --------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-10", dt.date(2026, 9, 10)),
        ("2026/09/10", dt.date(2026, 9, 10)),
        ("2026.09.10", dt.date(2026, 9, 10)),
        (dt.date(2026, 9, 10), dt.date(2026, 9, 10)),
        (dt.datetime(2026, 9, 10, 13, 30), dt.date(2026, 9, 10)),
        ("", None),
        (None, None),
    ],
)
def test_parse_date_valid(value, expected):
    parsed, ok = parse_date(value)
    assert ok is True
    assert parsed == expected


@pytest.mark.parametrize("value", ["10-09-2026", "not a date", "2026-13-40", 45900, 45900.0])
def test_parse_date_invalid(value):
    parsed, ok = parse_date(value)
    assert parsed is None
    assert ok is False


def test_invalid_date_produces_parse_error_but_keeps_row():
    task = one_task(task="작업", start="nope", end="2026-09-10")
    assert task.start_date is None
    assert task.end_date == dt.date(2026, 9, 10)
    assert "invalid_start_date" in task.parse_errors
    assert "invalid_end_date" not in task.parse_errors


# --- 8: progress ---------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("50%", 0.5),
        (0.5, 0.5),
        ("0.5", 0.5),
        (50, 0.5),
        ("50", 0.5),
        (1, 1.0),
        ("1", 1.0),
        (100, 1.0),
        ("100%", 1.0),
        (0, 0.0),
        ("", None),
        (None, None),
    ],
)
def test_parse_progress_valid(value, expected):
    parsed, ok = parse_progress(value)
    assert ok is True
    assert parsed == expected


@pytest.mark.parametrize("value", [-1, -0.5, "-10", 101, 150, "150", "150%", "abc", "%", True])
def test_parse_progress_invalid(value):
    parsed, ok = parse_progress(value)
    assert parsed is None
    assert ok is False


def test_invalid_progress_parse_error():
    task = one_task(task="작업", progress="200%")
    assert task.progress is None
    assert "invalid_progress" in task.parse_errors


# --- 9~10: status ------------------------------------------------------
def test_status_map_match_korean_and_english():
    cfg = make_config()
    assert one_task(cfg, task="a", status="완료").normalized_status == "DONE"
    assert one_task(cfg, task="b", status="  진행 ").normalized_status == "IN_PROGRESS"
    assert one_task(cfg, task="c", status="in progress").normalized_status == "IN_PROGRESS"


def test_status_unknown_is_preserved_not_parse_error():
    task = one_task(task="작업", status="검토중")
    assert task.normalized_status == NormalizedStatus.UNKNOWN.value
    assert task.raw_status == "검토중"
    assert task.parse_errors == []


def test_normalize_status_helper_empty():
    assert normalize_status(None, {}) == "UNKNOWN"
    assert normalize_status("   ", {"DONE": ["완료"]}) == "UNKNOWN"


# --- 11~12: dependency -----------------------------------------------
def test_dependency_comma():
    task = one_task(task="작업", deps="WBS-1, WBS-2 ,WBS-3")
    assert task.dependency == ["WBS-1", "WBS-2", "WBS-3"]


def test_dependency_semicolon():
    task = one_task(task="작업", deps="WBS-1;WBS-2")
    assert task.dependency == ["WBS-1", "WBS-2"]


def test_dependency_freeform_kept_as_single_item():
    assert parse_dependency("WBS-1 완료 후 진행") == ["WBS-1 완료 후 진행"]
    assert parse_dependency("") == []
    assert parse_dependency(None) == []


# --- 13: row_key -------------------------------------------------------
def test_row_key_reflects_data_start_row():
    cfg = make_config(header_row=2, data_start_row=3)
    rows = [["title only"], HEADER, row(task="A"), row(task="B")]
    result = normalize_sheet(rows, cfg)
    assert [t.row_key for t in result.tasks] == ["WBS!R3", "WBS!R4"]


# --- 14: source_identity_key ---------------------------------------
def test_source_identity_key_deterministic():
    a = compute_source_identity_key("작업", "설계", dt.date(2026, 9, 1))
    b = compute_source_identity_key("작업", "설계", dt.date(2026, 9, 1))
    c = compute_source_identity_key("작업", "설계", dt.date(2026, 9, 2))
    assert a == b
    assert a != c
    assert len(a) == 64


def test_source_identity_key_missing_fields_use_sentinel():
    key1 = compute_source_identity_key(None, None, None)
    key2 = compute_source_identity_key(None, None, None)
    assert key1 == key2
    assert key1 != compute_source_identity_key("작업", None, None)


# --- 15~21: row_hash -------------------------------------------------
def test_row_hash_deterministic():
    cfg = make_config()
    t1 = one_task(cfg, id="X", task="작업", owner="김", start="2026-09-01", end="2026-09-10",
                  progress="50%", status="진행", priority="높음", deps="A,B")
    t2 = one_task(cfg, id="X", task="작업", owner="김", start="2026-09-01", end="2026-09-10",
                  progress="50%", status="진행", priority="높음", deps="A,B")
    assert t1.row_hash == t2.row_hash


def test_row_hash_stable_across_row_position():
    # 16: 같은 내용이면 행 위치가 바뀌어도 row_hash 동일
    cfg = make_config()
    content = dict(task="작업", owner="김", start="2026-09-01", end="2026-09-10", status="진행")
    r_top = normalize_sheet([HEADER, row(**content), row(task="other")], cfg).tasks[0]
    r_mid = normalize_sheet([HEADER, row(task="other"), row(**content)], cfg).tasks[1]
    assert r_top.row_key != r_mid.row_key
    assert r_top.row_hash == r_mid.row_hash


def test_dependency_order_does_not_affect_row_hash():
    cfg = make_config()
    a = one_task(cfg, task="작업", deps="A,B,C")
    b = one_task(cfg, task="작업", deps="C,A,B")
    assert a.row_hash == b.row_hash


@pytest.mark.parametrize(
    "field,changed",
    [
        ("owner", dict(owner="이")),
        ("end", dict(end="2026-09-20")),
        ("progress", dict(progress="80%")),
        ("status", dict(status="완료")),
    ],
)
def test_row_hash_changes_on_meaningful_field(field, changed):
    cfg = make_config()
    base = dict(task="작업", owner="김", start="2026-09-01", end="2026-09-10",
                progress="50%", status="진행", priority="P1", deps="A")
    h_base = one_task(cfg, **base).row_hash
    h_changed = one_task(cfg, **{**base, **changed}).row_hash
    assert h_base != h_changed


def test_notes_change_does_not_affect_row_hash():
    # 21: notes는 현재 정책상 row_hash에서 제외
    cfg = make_config()
    base = dict(task="작업", owner="김", start="2026-09-01", end="2026-09-10")
    a = one_task(cfg, **base, notes="메모 A")
    b = one_task(cfg, **base, notes="완전히 다른 메모")
    assert a.notes != b.notes
    assert a.row_hash == b.row_hash


def test_row_key_not_in_row_hash_inputs():
    # 명시: compute_row_hash는 row_key/identity/parse_errors를 입력으로 받지 않는다
    h1 = compute_row_hash({"task_name": "작업", "owner": "김"})
    h2 = compute_row_hash({"task_name": "작업", "owner": "김", "row_key": "WBS!R99"})
    assert h1 == h2


# --- 22~24: 견고성 ----------------------------------------------------
def test_row_with_parse_errors_is_still_returned():
    task = one_task(task="", start="bad", progress="oops")
    assert isinstance(task, NormalizedTask)
    assert set(task.parse_errors) >= {"missing_task_name", "invalid_start_date", "invalid_progress"}


def test_header_mapping_failure_raises():
    cfg = make_config(columns={"task_name": "작업명"})
    with pytest.raises(WBSNormalizeError):
        normalize_sheet([["Task", "Owner"], ["x", "y"]], cfg)


def test_unmapped_extra_columns_are_ignored():
    cfg = make_config(columns={"task_name": "Task", "owner": "Owner"})
    rows = [["Task", "Owner", "SecretColumn"], ["작업", "김", "무시됨"]]
    result = normalize_sheet(rows, cfg)
    assert result.tasks[0].task_name == "작업"
    assert result.tasks[0].owner == "김"


def test_short_rows_are_handled():
    cfg = make_config(columns={"task_name": "Task", "owner": "Owner", "notes": "Notes"})
    rows = [["Task", "Owner", "Notes"], ["작업"]]  # owner/notes 셀 자체가 없음
    task = normalize_sheet(rows, cfg).tasks[0]
    assert task.task_name == "작업"
    assert task.owner is None
    assert task.notes is None


def test_normalize_result_shape():
    result = normalize_sheet([HEADER, row(task="A")], make_config())
    assert isinstance(result, NormalizationResult)
    assert isinstance(result.tasks, list)
    assert isinstance(result.skipped_rows, list)


# --- FakeSheetsClient -----------------------------------------------
def test_fake_sheets_client_modified_time_set_get():
    fake = FakeSheetsClient()
    assert fake.get_modified_time("S") == "1970-01-01T00:00:00.000Z"
    fake.set_modified_time("2026-09-10T00:00:00.000Z")
    assert fake.get_modified_time("S") == "2026-09-10T00:00:00.000Z"
    assert fake.get_modified_time_calls == 2
    assert fake.last_get_modified_time_args == ("S",)


def test_fake_sheets_client_values_set_get_and_counters():
    fake = FakeSheetsClient()
    assert fake.get_values("S", "WBS") == []
    fake.set_values([["Task"], ["작업"]])
    assert fake.get_values("S", "WBS") == [["Task"], ["작업"]]
    assert fake.get_values_calls == 2
    assert fake.last_get_values_args == ("S", "WBS")


def test_fake_sheets_client_returns_copies():
    fake = FakeSheetsClient()
    fake.set_values([["Task"], ["작업"]])
    got = fake.get_values("S", "WBS")
    got[0].append("mutated")
    assert fake.get_values("S", "WBS") == [["Task"], ["작업"]]


def test_fake_sheets_client_has_no_network_or_content_methods():
    fake = FakeSheetsClient()
    for attr in ("download", "get_file_content", "export", "_service", "batch_update"):
        assert not hasattr(fake, attr)
