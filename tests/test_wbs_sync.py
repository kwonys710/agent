"""WBS SOURCE CHANGE sync 테스트 (Commit 3).

Google API 실제 호출 없음 — FakeSheetsClient + deterministic id_factory 사용.
Daily rule / Todo candidate / Claude / Slack 은 이 커밋 범위 밖.
"""
import pytest

from engine.config.loader import DriveConfig, ProjectConfig
from engine.wbs.config import WBSConfig
from engine.wbs.state import load_states
from engine.wbs.sync import (
    WBSBootstrapAlreadyDone,
    WBSBootstrapRequired,
    WBSNotConfigured,
    run_wbs_bootstrap,
    run_wbs_sync,
)
from tests.fakes import FakeSheetsClient

HEADER = ["ID", "Task", "Phase", "Owner", "Start", "End", "Progress", "Status", "Notes"]
_ORDER = ["id", "task", "phase", "owner", "start", "end", "progress", "status", "notes"]

NO_TASK_ID_COLUMNS = {
    "task_name": "Task",
    "phase": "Phase",
    "owner": "Owner",
    "start_date": "Start",
    "end_date": "End",
    "progress": "Progress",
    "status": "Status",
    "notes": "Notes",
}


def rows(*tasks):
    out = [list(HEADER)]
    for task in tasks:
        out.append([task.get(key, "") for key in _ORDER])
    return out


def make_wbs_config(**overrides):
    raw = {
        "spreadsheet_id": "SHEET1",
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
            "notes": "Notes",
        },
        "status_map": {"DONE": ["완료"], "IN_PROGRESS": ["진행"], "BLOCKED": ["지연"]},
    }
    raw.update(overrides)
    return WBSConfig.from_raw(raw)


def make_config(wbs="__default__"):
    return ProjectConfig(
        project_id="demo",
        project_name="Demo",
        drive=DriveConfig(
            drive_type="my_drive", root_folder_id="ROOT",
            include_subfolders=True, excluded_folder_ids=[],
        ),
        scope_config_version=1,
        wbs=make_wbs_config() if wbs == "__default__" else wbs,
    )


class SeqIds:
    def __init__(self, prefix="t"):
        self.n = 0
        self.prefix = prefix

    def __call__(self):
        self.n += 1
        return f"{self.prefix}{self.n}"


def client_with(modified, *tasks):
    client = FakeSheetsClient()
    client.set_modified_time(modified)
    client.set_values(rows(*tasks))
    return client


def states_map(conn, key="internal_task_id"):
    return {getattr(s, key): s for s in load_states(conn, "demo", "SHEET1", "WBS")}


def checkpoint(conn):
    return conn.execute(
        "SELECT * FROM source_checkpoints WHERE project_id='demo' AND source_type='wbs'"
    ).fetchone()


# --- 1~2: bootstrap --------------------------------------------------------
def test_bootstrap_creates_state_and_checkpoint(conn):
    client = client_with("m1", {"id": "W-1", "task": "A", "start": "2026-09-01"}, {"id": "W-2", "task": "B"})
    report = run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    assert report["result"] == "SUCCESS"
    assert sorted(report["new"]) == ["t1", "t2"]
    assert client.get_values_calls == 1
    assert {s.internal_task_id for s in load_states(conn, "demo", "SHEET1", "WBS")} == {"t1", "t2"}
    assert checkpoint(conn)["last_page_token"] == "m1"
    assert checkpoint(conn)["last_run_status"] == "success"


def test_bootstrap_rerun_refused_without_override(conn):
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    with pytest.raises(WBSBootstrapAlreadyDone):
        run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())


def test_bootstrap_override_does_not_truncate_state(conn):
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    client.set_modified_time("m2")
    report = run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds("x"), override=True)
    # 같은 task 는 identity 로 재매칭되어 유지 — 새 id 발급 아님
    assert report["new"] == []
    assert {s.internal_task_id for s in load_states(conn, "demo", "SHEET1", "WBS")} == {"t1"}


# --- 3: unchanged source -------------------------------------------------
def test_sync_unchanged_source_skips_get_values(conn):
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    assert client.get_values_calls == 1
    report = run_wbs_sync(conn, make_config(), client)
    assert report["unchanged_source"] is True
    assert client.get_values_calls == 1
    assert report["new"] == [] and report["modified"] == [] and report["deactivated"] == []
    assert checkpoint(conn)["last_page_token"] == "m1"


# --- 4~7: identity 유지 --------------------------------------------------
def test_sync_new_task_keeps_existing_ids(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A"}, {"id": "W-2", "task": "B"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert report["new"] == ["t2"]
    assert report["unchanged"] == ["t1"]


def test_source_task_id_unique_match_keeps_internal_id_on_move(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"}, {"id": "W-2", "task": "B"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-2", "task": "B"}, {"id": "W-1", "task": "A"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert report["new"] == []
    assert sorted(report["unchanged"]) == ["t1", "t2"]
    by_sid = states_map(conn, "source_task_id")
    assert by_sid["W-1"].internal_task_id == "t1"
    assert by_sid["W-1"].row_key == "WBS!R3"


def test_source_task_id_changed_but_identity_key_unique_keeps_id(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "설계", "phase": "P1", "start": "2026-09-01"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-9", "task": "설계", "phase": "P1", "start": "2026-09-01"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert report["new"] == []
    states = load_states(conn, "demo", "SHEET1", "WBS")
    assert len(states) == 1
    assert states[0].internal_task_id == "t1"
    assert states[0].source_task_id == "W-9"


def test_row_move_only_produces_no_new_or_modified(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"}, {"id": "W-2", "task": "B"}, {"id": "W-3", "task": "C"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    before = {s.internal_task_id: s.row_hash for s in load_states(conn, "demo", "SHEET1", "WBS")}
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-3", "task": "C"}, {"id": "W-1", "task": "A"}, {"id": "W-2", "task": "B"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert report["new"] == []
    assert report["modified"] == []
    assert sorted(report["unchanged"]) == ["t1", "t2", "t3"]
    after = {s.internal_task_id: s.row_hash for s in load_states(conn, "demo", "SHEET1", "WBS")}
    assert before == after


# --- 8~10: modified / diff --------------------------------------------
def test_owner_change_is_modified_and_persisted(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "owner": "김"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "owner": "이"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert len(report["modified"]) == 1
    entry = report["modified"][0]
    assert entry["internal_task_id"] == "t1"
    assert "owner" in entry["changed_fields"]
    assert entry["row_hash_changed"] is True
    assert load_states(conn, "demo", "SHEET1", "WBS")[0].owner == "이"


def test_end_date_change_is_modified(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": "2026-09-10"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "end": "2026-09-20"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert "end_date" in report["modified"][0]["changed_fields"]
    assert load_states(conn, "demo", "SHEET1", "WBS")[0].end_date == "2026-09-20"


def test_notes_only_change_modified_without_row_hash_change(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "notes": "n1"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "notes": "완전히 다른 메모"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert len(report["modified"]) == 1
    entry = report["modified"][0]
    assert entry["changed_fields"] == ["notes"]
    assert entry["row_hash_changed"] is False
    assert load_states(conn, "demo", "SHEET1", "WBS")[0].notes == "완전히 다른 메모"


# --- 11~12: duplicate / ambiguous ------------------------------------
def test_duplicate_source_task_id_resolved_via_identity_key(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"}, {"id": "W-1", "task": "B"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A"}, {"id": "W-1", "task": "B"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=SeqIds("z"))
    assert report["new"] == []
    assert report["ambiguous"] == []
    assert {s.task_name: s.internal_task_id for s in load_states(conn, "demo", "SHEET1", "WBS")} == {
        "A": "t1", "B": "t2",
    }


def test_duplicate_identity_key_is_ambiguous_not_arbitrary(conn):
    cfg = make_config(make_wbs_config(columns=NO_TASK_ID_COLUMNS))
    dup = {"task": "공통작업", "phase": "P", "start": "2026-09-01"}
    client = FakeSheetsClient()
    client.set_modified_time("m1")
    client.set_values(rows({**dup, "owner": "김"}, {**dup, "owner": "이"}))
    run_wbs_bootstrap(conn, cfg, client, id_factory=SeqIds())
    client.set_modified_time("m2")
    client.set_values(rows({**dup, "owner": "김"}, {**dup, "owner": "이"}, {**dup, "owner": "박"}))
    report = run_wbs_sync(conn, cfg, client, id_factory=SeqIds("n"))
    assert len(report["ambiguous"]) == 3
    assert sorted(report["deactivated"]) == ["t1", "t2"]
    for state in load_states(conn, "demo", "SHEET1", "WBS", active=True):
        assert "ambiguous_identity_match" in state.parse_errors


# --- 13~15: task_id 없는 업무 ----------------------------------------
def test_no_task_id_new_task_gets_internal_id(conn):
    cfg = make_config(make_wbs_config(columns=NO_TASK_ID_COLUMNS))
    client = FakeSheetsClient()
    client.set_modified_time("m1")
    client.set_values(rows({"task": "작업1", "start": "2026-09-01"}))
    report = run_wbs_bootstrap(conn, cfg, client, id_factory=SeqIds())
    assert report["new"] == ["t1"]
    state = load_states(conn, "demo", "SHEET1", "WBS")[0]
    assert state.internal_task_id == "t1"
    assert state.source_task_id is None


def test_no_task_id_row_move_keeps_id_via_identity_key(conn):
    cfg = make_config(make_wbs_config(columns=NO_TASK_ID_COLUMNS))
    ids = SeqIds()
    client = FakeSheetsClient()
    client.set_modified_time("m1")
    client.set_values(rows({"task": "작업1", "start": "2026-09-01"}, {"task": "작업2", "start": "2026-09-02"}))
    run_wbs_bootstrap(conn, cfg, client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"task": "작업2", "start": "2026-09-02"}, {"task": "작업1", "start": "2026-09-01"}))
    report = run_wbs_sync(conn, cfg, client, id_factory=SeqIds("n"))
    assert report["new"] == []
    assert {s.task_name: s.internal_task_id for s in load_states(conn, "demo", "SHEET1", "WBS")} == {
        "작업1": "t1", "작업2": "t2",
    }


def test_no_task_id_task_name_change_is_new_and_old_deactivated(conn):
    cfg = make_config(make_wbs_config(columns=NO_TASK_ID_COLUMNS))
    ids = SeqIds()
    client = FakeSheetsClient()
    client.set_modified_time("m1")
    client.set_values(rows({"task": "초안 작성", "start": "2026-09-01", "owner": "김"}))
    run_wbs_bootstrap(conn, cfg, client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"task": "초안 작성 및 검토", "start": "2026-09-01", "owner": "김"}))
    report = run_wbs_sync(conn, cfg, client, id_factory=SeqIds("n"))
    assert report["new"] == ["n1"]
    assert report["deactivated"] == ["t1"]
    active = load_states(conn, "demo", "SHEET1", "WBS", active=True)
    assert [s.task_name for s in active] == ["초안 작성 및 검토"]


# --- 16~17: deactivate / reactivate --------------------------------
def test_removed_task_is_deactivated(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"}, {"id": "W-2", "task": "B"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert report["deactivated"] == ["t2"]
    all_states = states_map(conn)
    assert all_states["t2"].is_active is False
    assert all_states["t1"].is_active is True


def test_returned_task_is_reactivated_with_same_id(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"}, {"id": "W-2", "task": "B"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A"}))
    run_wbs_sync(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m3")
    client.set_values(rows({"id": "W-1", "task": "A"}, {"id": "W-2", "task": "B"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=SeqIds("n"))
    assert len(report["reactivated"]) == 1
    assert report["reactivated"][0]["internal_task_id"] == "t2"
    assert report["new"] == []
    assert {s.internal_task_id for s in load_states(conn, "demo", "SHEET1", "WBS", active=True)} == {"t1", "t2"}


# --- 18~19: parse errors / ambiguous marker ------------------------
def test_invalid_values_keep_state_and_persist_parse_errors(conn):
    client = client_with("m1", {"id": "W-1", "task": "A", "start": "bad", "progress": "999%"})
    report = run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    assert len(report["new"]) == 1
    state = load_states(conn, "demo", "SHEET1", "WBS")[0]
    assert set(state.parse_errors) >= {"invalid_start_date", "invalid_progress"}
    assert state.start_date is None and state.progress is None


def test_ambiguous_identity_appends_parse_error_and_new_id(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "DUP", "task": "A"}, {"id": "DUP", "task": "B"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "DUP", "task": "C"}, {"id": "DUP", "task": "A"}, {"id": "DUP", "task": "B"}))
    report = run_wbs_sync(conn, make_config(), client, id_factory=SeqIds("n"))
    assert len(report["ambiguous"]) == 1
    amb_id = report["ambiguous"][0]["internal_task_id"]
    assert amb_id.startswith("n")
    amb_state = [s for s in load_states(conn, "demo", "SHEET1", "WBS", active=True)
                 if s.internal_task_id == amb_id][0]
    assert "ambiguous_identity_match" in amb_state.parse_errors
    # A, B 는 정상적으로 기존 id 유지
    assert {s.task_name: s.internal_task_id for s in load_states(conn, "demo", "SHEET1", "WBS", active=True)
            if s.task_name in ("A", "B")} == {"A": "t1", "B": "t2"}


# --- claimed collision regression ----------------------------------
def test_two_rows_pointing_to_same_existing_state_second_is_ambiguous(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "start": "2026-09-01"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)  # -> t1
    client.set_modified_time("m2")
    # 두 행 모두 W-1 / 동일 내용 → 둘 다 기존 t1 을 identity 근거로 가리킨다.
    client.set_values(rows(
        {"id": "W-1", "task": "A", "start": "2026-09-01"},
        {"id": "W-1", "task": "A", "start": "2026-09-01"},
    ))
    report = run_wbs_sync(conn, make_config(), client, id_factory=SeqIds("n"))

    used_existing = report["unchanged"] + [e["internal_task_id"] for e in report["modified"]]
    assert used_existing == ["t1"]  # 정확히 1개만 기존 id 유지

    assert len(report["ambiguous"]) == 1
    amb = report["ambiguous"][0]
    assert amb["internal_task_id"] != "t1"          # 기존 id 재사용 안 함
    assert amb["internal_task_id"].startswith("n")  # 새 id 발급
    assert amb["reason"] == "existing_task_already_claimed"
    assert report["new"] == []                      # 일반 new 로 분류되지 않음

    active = load_states(conn, "demo", "SHEET1", "WBS", active=True)
    assert len(active) == 2
    amb_state = [s for s in active if s.internal_task_id == amb["internal_task_id"]][0]
    assert "ambiguous_identity_match" in amb_state.parse_errors


@pytest.mark.parametrize("order", [("김", "이"), ("이", "김")])
def test_claimed_collision_invariant_regardless_of_row_order(conn, order):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "start": "2026-09-01", "owner": "원본"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)  # -> t1
    client.set_modified_time("m2")
    client.set_values(rows(
        {"id": "W-1", "task": "A", "start": "2026-09-01", "owner": order[0]},
        {"id": "W-1", "task": "A", "start": "2026-09-01", "owner": order[1]},
    ))
    report = run_wbs_sync(conn, make_config(), client, id_factory=SeqIds("n"))

    # invariant: 정확히 1개만 t1 유지, 나머지 정확히 1개는 ambiguous, 일반 new 0
    used_existing = report["unchanged"] + [e["internal_task_id"] for e in report["modified"]]
    assert used_existing == ["t1"]
    assert len(report["ambiguous"]) == 1
    assert report["ambiguous"][0]["internal_task_id"] != "t1"
    assert report["ambiguous"][0]["reason"] == "existing_task_already_claimed"
    assert report["new"] == []
    # 기존 t1 이 두 incoming row 에 중복 매칭되지 않음
    assert len({s.internal_task_id for s in load_states(conn, "demo", "SHEET1", "WBS", active=True)}) == 2


# --- 20~23: checkpoint / failure --------------------------------------
def test_checkpoint_advances_only_after_state_applied(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    assert checkpoint(conn)["last_page_token"] == "m1"
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "owner": "이"}))
    run_wbs_sync(conn, make_config(), client, id_factory=ids)
    assert checkpoint(conn)["last_page_token"] == "m2"


def test_get_values_exception_leaves_state_and_checkpoint_intact(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)

    class Boom(FakeSheetsClient):
        def get_values(self, *args, **kwargs):
            raise RuntimeError("network down")

    boom = Boom()
    boom.set_modified_time("m2")
    with pytest.raises(RuntimeError):
        run_wbs_sync(conn, make_config(), boom, id_factory=ids)

    cp = checkpoint(conn)
    assert cp["last_page_token"] == "m1"
    assert cp["last_run_status"] == "failed"
    assert "network down" in cp["last_error"]
    assert len(load_states(conn, "demo", "SHEET1", "WBS")) == 1


def test_normalize_exception_leaves_state_and_checkpoint_intact(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    client.set_modified_time("m2")
    client.set_values([["WRONG", "HEADERS"], ["x", "y"]])
    with pytest.raises(Exception):
        run_wbs_sync(conn, make_config(), client, id_factory=ids)
    cp = checkpoint(conn)
    assert cp["last_page_token"] == "m1"
    assert cp["last_run_status"] == "failed"


def test_db_write_exception_rolls_back_state_and_checkpoint(conn, monkeypatch):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "owner": "김"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)

    import engine.wbs.sync as sync_module

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sync_module, "update_task_state", boom)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "owner": "이"}))
    with pytest.raises(RuntimeError):
        run_wbs_sync(conn, make_config(), client, id_factory=ids)

    cp = checkpoint(conn)
    assert cp["last_page_token"] == "m1"
    assert cp["last_run_status"] == "failed"
    assert load_states(conn, "demo", "SHEET1", "WBS")[0].owner == "김"


# --- 24~26: misc ----------------------------------------------------
def test_bootstrap_saves_duplicate_source_task_id_rows(conn):
    client = client_with("m1", {"id": "W-1", "task": "A"}, {"id": "W-1", "task": "B"})
    report = run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    assert len(report["new"]) == 2
    states = load_states(conn, "demo", "SHEET1", "WBS")
    assert {s.source_task_id for s in states} == {"W-1"}
    assert len({s.internal_task_id for s in states}) == 2


def test_skipped_blank_rows_reported(conn):
    client = FakeSheetsClient()
    client.set_modified_time("m1")
    client.set_values([list(HEADER), ["W-1", "A", "", ""], ["", "", ""], ["W-2", "B"]])
    report = run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    assert report["skipped_rows"] == [3]
    assert len(report["new"]) == 2


def test_client_call_counters_across_runs(conn):
    client = client_with("m1", {"id": "W-1", "task": "A"})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=SeqIds())
    assert (client.get_modified_time_calls, client.get_values_calls) == (1, 1)

    run_wbs_sync(conn, make_config(), client)  # unchanged
    assert (client.get_modified_time_calls, client.get_values_calls) == (2, 1)

    client.set_modified_time("m2")
    run_wbs_sync(conn, make_config(), client, id_factory=SeqIds("n"))
    assert (client.get_modified_time_calls, client.get_values_calls) == (3, 2)


# --- guards --------------------------------------------------------
def test_sync_without_bootstrap_raises(conn):
    with pytest.raises(WBSBootstrapRequired):
        run_wbs_sync(conn, make_config(), FakeSheetsClient())


def test_bootstrap_without_wbs_config_raises(conn):
    cfg = make_config(wbs=None)
    with pytest.raises(WBSNotConfigured):
        run_wbs_bootstrap(conn, cfg, FakeSheetsClient())


# --- CLI ----------------------------------------------------------
def test_cli_reports_client_not_configured(conn, monkeypatch, capsys):
    import scripts.wbs_sync as cli

    monkeypatch.setattr(cli, "load_project_config", lambda project: make_config())
    rc = cli.run_cli(["--project", "demo"], connection=conn)
    assert rc == 3
    assert "인증 배선" in capsys.readouterr().out


def test_cli_runs_bootstrap_with_injected_fake_client(conn, monkeypatch, capsys):
    import scripts.wbs_sync as cli

    monkeypatch.setattr(cli, "load_project_config", lambda project: make_config())
    client = client_with("m1", {"id": "W-1", "task": "A"})
    rc = cli.run_cli(["--project", "demo", "--bootstrap"], client_factory=lambda: client, connection=conn)
    assert rc == 0
    out = capsys.readouterr().out
    assert "WBS Bootstrap" in out
    assert "New: 1" in out
    assert load_states(conn, "demo", "SHEET1", "WBS")


def test_cli_skips_project_without_wbs(conn, monkeypatch, capsys):
    import scripts.wbs_sync as cli

    monkeypatch.setattr(cli, "load_project_config", lambda project: make_config(wbs=None))
    rc = cli.run_cli(["--project", "demo"], connection=conn)
    assert rc == 0
    assert "wbs 섹션이 없" in capsys.readouterr().out
