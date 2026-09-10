"""WBSConfig 파싱/검증 + ProjectConfig 최소 확장 테스트 (Commit 1).

Sheets 호출/normalize/sync/rule은 이 단계에 없다 — 설정 로딩/검증만 검증한다.
"""
import textwrap

import pytest

from engine.config import loader
from engine.config.loader import ConfigError, load_project_config
from engine.wbs.config import WBSConfig, WBSConfigError, DEFAULT_TIMEZONE


def _minimal_wbs_raw(**overrides):
    raw = {
        "spreadsheet_id": "TEST_SHEET_ID",
        "sheet_name": "WBS",
        "columns": {"task_name": "작업명"},
    }
    raw.update(overrides)
    return raw


# ---------------------------------------------------------------------------
# WBSConfig.from_raw
# ---------------------------------------------------------------------------
def test_from_raw_minimal_ok():
    cfg = WBSConfig.from_raw(_minimal_wbs_raw())
    assert isinstance(cfg, WBSConfig)
    assert cfg.spreadsheet_id == "TEST_SHEET_ID"
    assert cfg.sheet_name == "WBS"
    assert cfg.columns == {"task_name": "작업명"}


def test_task_id_column_mapping_is_optional():
    # C: task_id 매핑이 없어도 로드 가능해야 한다.
    cfg = WBSConfig.from_raw(_minimal_wbs_raw(columns={"task_name": "작업명"}))
    assert "task_id" not in cfg.columns


def test_missing_task_name_mapping_raises():
    # D: 필수 최소 논리 컬럼(task_name) 매핑 누락 → 명확한 WBSConfigError
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(_minimal_wbs_raw(columns={"owner": "담당자"}))


def test_missing_spreadsheet_id_raises():
    raw = _minimal_wbs_raw()
    del raw["spreadsheet_id"]
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(raw)


def test_placeholder_spreadsheet_id_raises():
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(_minimal_wbs_raw(spreadsheet_id="CHANGE_ME"))


def test_spreadsheet_id_from_env(monkeypatch):
    monkeypatch.setenv("PM_DEMO_WBS_SPREADSHEET_ID", "ENV_SHEET_ID")
    raw = _minimal_wbs_raw(spreadsheet_id="CHANGE_ME")
    cfg = WBSConfig.from_raw(raw, project_id="demo")
    assert cfg.spreadsheet_id == "ENV_SHEET_ID"


def test_missing_sheet_name_raises():
    raw = _minimal_wbs_raw()
    del raw["sheet_name"]
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(raw)


def test_wbs_section_must_be_mapping():
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(["not", "a", "dict"])


# ---------------------------------------------------------------------------
# timezone (E / E2 / F)
# ---------------------------------------------------------------------------
def test_timezone_defaults_to_asia_seoul():
    cfg = WBSConfig.from_raw(_minimal_wbs_raw())
    assert cfg.timezone == "Asia/Seoul"
    assert DEFAULT_TIMEZONE == "Asia/Seoul"
    # 검증된 값으로 실제 ZoneInfo 생성이 가능해야 한다.
    assert cfg.zoneinfo() is not None


@pytest.mark.parametrize("tz", ["Asia/Seoul", "UTC", "America/New_York", "Europe/Berlin"])
def test_valid_timezones_accepted(tz):
    cfg = WBSConfig.from_raw(_minimal_wbs_raw(timezone=tz))
    assert cfg.timezone == tz
    assert str(cfg.zoneinfo()) == tz


@pytest.mark.parametrize("tz", ["Invalid/Timezone", "seoul", "", "   ", "Asia/Atlantis"])
def test_invalid_timezone_raises(tz):
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(_minimal_wbs_raw(timezone=tz))


# ---------------------------------------------------------------------------
# status_map / numeric coercion (G)
# ---------------------------------------------------------------------------
def test_status_map_parsing():
    raw = _minimal_wbs_raw(
        status_map={
            "DONE": ["완료", "종료"],
            "IN_PROGRESS": ["진행", "진행중"],
            "BLOCKED": ["지연"],
        }
    )
    cfg = WBSConfig.from_raw(raw)
    assert cfg.status_map == {
        "DONE": ["완료", "종료"],
        "IN_PROGRESS": ["진행", "진행중"],
        "BLOCKED": ["지연"],
    }


def test_status_map_must_be_mapping():
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(_minimal_wbs_raw(status_map=["완료"]))


def test_numeric_fields_defaults_and_override():
    cfg = WBSConfig.from_raw(_minimal_wbs_raw())
    assert cfg.header_row == 1
    assert cfg.data_start_row == 2
    assert cfg.due_soon_days == 3
    assert cfg.progress_done_threshold == 1.0
    assert cfg.progress_change_min_delta == 0.1

    cfg2 = WBSConfig.from_raw(_minimal_wbs_raw(due_soon_days=5, progress_done_threshold=0.95))
    assert cfg2.due_soon_days == 5
    assert cfg2.progress_done_threshold == 0.95


def test_invalid_numeric_field_raises():
    with pytest.raises(WBSConfigError):
        WBSConfig.from_raw(_minimal_wbs_raw(due_soon_days="soon"))


def test_change_significant_fields_default():
    cfg = WBSConfig.from_raw(_minimal_wbs_raw())
    assert cfg.change_significant_fields == [
        "start_date",
        "end_date",
        "owner",
        "normalized_status",
        "progress",
    ]


# ---------------------------------------------------------------------------
# ProjectConfig 통합 (A / B)
# ---------------------------------------------------------------------------
def _write_config(tmp_path, monkeypatch, project_id, body):
    monkeypatch.setattr(loader, "PROJECTS_DIR", tmp_path)
    proj_dir = tmp_path / project_id
    proj_dir.mkdir()
    (proj_dir / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


DRIVE_ONLY = """
    project_id: demo
    project_name: "Demo"
    drive:
      drive_type: my_drive
      root_folder_id: ROOT123
      include_subfolders: true
      excluded_folder_ids: []
    scope:
      config_version: 1
"""


def test_project_without_wbs_section_loads_and_wbs_is_none(tmp_path, monkeypatch):
    # A: wbs 섹션 없음 → config.wbs is None, 기존 drive config 정상 로드
    _write_config(tmp_path, monkeypatch, "demo", DRIVE_ONLY)
    cfg = load_project_config("demo")
    assert cfg.wbs is None
    assert cfg.drive.root_folder_id == "ROOT123"
    assert cfg.scope_config_version == 1


def test_project_with_wbs_section_parses_wbsconfig(tmp_path, monkeypatch):
    # B: wbs 섹션 있음 → WBSConfig 정상 생성
    _write_config(
        tmp_path,
        monkeypatch,
        "demo",
        DRIVE_ONLY
        + """
    wbs:
      spreadsheet_id: DEMO_SHEET
      sheet_name: WBS
      timezone: Asia/Seoul
      columns:
        task_name: 작업명
        owner: 담당자
      status_map:
        DONE: ["완료"]
""",
    )
    cfg = load_project_config("demo")
    assert isinstance(cfg.wbs, WBSConfig)
    assert cfg.wbs.spreadsheet_id == "DEMO_SHEET"
    assert cfg.wbs.timezone == "Asia/Seoul"
    assert cfg.wbs.columns == {"task_name": "작업명", "owner": "담당자"}


def test_project_with_invalid_wbs_timezone_raises(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        monkeypatch,
        "demo",
        DRIVE_ONLY
        + """
    wbs:
      spreadsheet_id: DEMO_SHEET
      sheet_name: WBS
      timezone: Invalid/Timezone
      columns:
        task_name: 작업명
""",
    )
    with pytest.raises(WBSConfigError):
        load_project_config("demo")


def test_missing_drive_section_still_errors(tmp_path, monkeypatch):
    # 이번 commit은 drive를 optional로 만들지 않는다 — 기존 동작 유지 확인.
    _write_config(
        tmp_path,
        monkeypatch,
        "demo",
        """
    project_id: demo
    project_name: "Demo"
    scope:
      config_version: 1
""",
    )
    with pytest.raises(ConfigError):
        load_project_config("demo")
