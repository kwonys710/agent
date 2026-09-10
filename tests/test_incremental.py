import dataclasses

import pytest

from engine.drive.bootstrap import run_bootstrap
from engine.drive.incremental import BootstrapRequired, run_incremental
from engine.drive.scope import ProjectScopeFilter
from tests.fakes import FakeDriveClient


def _bootstrap_with_one_file(conn, config):
    fake = FakeDriveClient()
    fake.add_file("F1", "0907회의록.pdf", "application/pdf", parent="ROOT", checksum="h1")
    run_bootstrap(conn, config, fake)
    return fake


# Test: Incremental이 Bootstrap을 대신 실행하지 않음 ---------------------------------

def test_incremental_requires_bootstrap(conn, config):
    fake = FakeDriveClient()
    with pytest.raises(BootstrapRequired):
        run_incremental(conn, config, fake)


# Test B: 변경 없음 ------------------------------------------------------------------

def test_no_changes_detected(conn, config):
    fake = _bootstrap_with_one_file(conn, config)

    report = run_incremental(conn, config, fake)

    assert report["changes_received"] == 0
    assert report["new"] == []
    assert report["modified"] == []
    assert report["content_downloads"] == 0
    assert report["claude_api_calls"] == 0
    assert report["result"] == "SUCCESS"


# Test C: 신규 파일 1개 — 기존 파일은 재처리되지 않음 ----------------------------------

def test_new_file_detected_existing_file_untouched(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.add_file("F2", "검색대상정의서.xlsx", "application/vnd.ms-excel", parent="ROOT", checksum="h2")

    report = run_incremental(conn, config, fake)

    assert report["new"] == ["검색대상정의서.xlsx"]
    assert report["modified"] == []

    f1 = conn.execute("SELECT internal_content_version FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert f1["internal_content_version"] == 1  # 기존 파일은 그대로


# Test D: 기존 파일 수정 --------------------------------------------------------------

def test_modified_binary_file_bumps_content_version(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.modify_file("F1", md5Checksum="h1-changed", modifiedTime="t1")

    report = run_incremental(conn, config, fake)

    assert report["modified"] == ["0907회의록.pdf"]
    row = conn.execute("SELECT internal_content_version FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert row["internal_content_version"] == 2


# Test E: Rename — 내용 재처리 없음 ----------------------------------------------------

def test_rename_does_not_trigger_modified(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.modify_file("F1", name="0907회의록_v2.pdf")

    report = run_incremental(conn, config, fake)

    assert report["renamed"] == ["0907회의록_v2.pdf"]
    assert report["modified"] == []
    row = conn.execute("SELECT internal_content_version FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert row["internal_content_version"] == 1


# Test F: 프로젝트 내부 이동 — 내용 재처리 없음 ------------------------------------------

def test_move_within_project_does_not_trigger_modified(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.add_folder("SUB1", "회의록", parent="ROOT")

    report_after_folder_add = run_incremental(conn, config, fake)  # SUB1 폴더 변경 이벤트 소비
    assert report_after_folder_add["result"] == "SUCCESS"

    fake.modify_file("F1", parents=["SUB1"])
    report = run_incremental(conn, config, fake)

    assert report["moved"] == ["0907회의록.pdf"]
    assert report["modified"] == []


# Test G: 프로젝트 밖으로 이동 — Registry는 유지, active 대상에서만 제외 --------------------

def test_move_out_of_project_scope_keeps_registry_row(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.modify_file("F1", parents=["OUTSIDE"])

    report = run_incremental(conn, config, fake)

    assert report["out_of_scope"] == 1
    row = conn.execute("SELECT in_project_scope FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert row is not None
    assert row["in_project_scope"] == 0


# 실제 Drive 통합 테스트에서 발견된 re-entry 상태 불일치 재현 테스트 -------------------------------
# scope 밖 → 안으로 같은 drive_file_id가 돌아왔을 때 in_project_scope는 복구되지만
# processing_status가 'out_of_scope'로 남아있던 버그의 regression test.

def test_file_scope_reentry_restores_processing_status(conn, config):
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT, checksum="h1"

    # 1) scope 밖으로 이동
    fake.modify_file("F1", parents=["OUTSIDE"])
    out_report = run_incremental(conn, config, fake)
    assert out_report["out_of_scope"] == 1

    left = conn.execute(
        "SELECT in_project_scope, processing_status, is_deleted FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert left["in_project_scope"] == 0
    assert left["processing_status"] == "out_of_scope"
    assert left["is_deleted"] == 0

    # 2) 같은 drive_file_id가 다시 프로젝트(ROOT)로 이동 — 내용은 그대로(checksum 불변)
    fake.modify_file("F1", parents=["ROOT"])
    back_report = run_incremental(conn, config, fake)

    rows = conn.execute("SELECT * FROM files WHERE drive_file_id = 'F1'").fetchall()
    assert len(rows) == 1, "중복 registry row가 생성되면 안 된다"

    restored = rows[0]
    assert restored["in_project_scope"] == 1
    assert restored["processing_status"] != "out_of_scope", (
        "scope 재진입 후에도 processing_status가 'out_of_scope'로 남아있다 (재현된 버그)"
    )
    assert restored["processing_status"] == "registered"
    assert restored["is_deleted"] == 0
    assert restored["internal_content_version"] == 1  # 내용 변경이 없었으므로 버전 증가 없음

    assert back_report["content_downloads"] == 0
    assert back_report["claude_api_calls"] == 0


def test_file_scope_reentry_to_same_original_folder_is_detected(conn, config):
    """parent_folder_id가 scope 이탈 시점에 갱신되지 않으면, 원래 있던 바로 그 폴더로
    되돌아왔을 때 reasons(diff)가 비어 재진입 자체를 놓칠 수 있다 — 이를 방지하는 회귀 테스트."""
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT

    fake.modify_file("F1", parents=["OUTSIDE"])
    run_incremental(conn, config, fake)

    # 나갔던 것과 동일한 원래 폴더(ROOT)로 정확히 되돌아옴
    fake.modify_file("F1", parents=["ROOT"])
    report = run_incremental(conn, config, fake)

    row = conn.execute(
        "SELECT in_project_scope, processing_status FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert row["in_project_scope"] == 1, "원래 폴더로 되돌아왔는데도 out_of_scope에 갇히면 안 된다"
    assert row["processing_status"] == "registered"
    assert report["scope_matched"] == 1


# 삭제 --------------------------------------------------------------------------------

def test_deleted_file_marks_registry_without_removing_row(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.trash_file("F1")

    report = run_incremental(conn, config, fake)

    assert report["deleted"] == ["0907회의록.pdf"]
    row = conn.execute("SELECT is_deleted FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert row["is_deleted"] == 1


# 삭제 상태 전이(Fix A) — scope 안 파일 삭제 시 in_project_scope도 0으로 내려간다 -----------------

def test_trash_transitions_scope_out_and_keeps_registry_row(conn, config):
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT, checksum="h1", scope=1

    before = conn.execute(
        "SELECT internal_file_id, drive_file_id, internal_content_version, checksum, "
        "in_project_scope, is_deleted, processing_status FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert before["in_project_scope"] == 1
    assert before["is_deleted"] == 0

    fake.trash_file("F1")
    report = run_incremental(conn, config, fake)

    assert report["deleted"] == ["0907회의록.pdf"]
    assert report["content_downloads"] == 0
    assert report["claude_api_calls"] == 0

    rows = conn.execute("SELECT * FROM files WHERE drive_file_id = 'F1'").fetchall()
    assert len(rows) == 1, "삭제로 registry row가 사라지거나 중복되면 안 된다"
    after = rows[0]
    assert after["processing_status"] == "deleted"
    assert after["is_deleted"] == 1
    assert after["in_project_scope"] == 0
    assert after["drive_file_id"] == "F1"
    assert after["internal_file_id"] == before["internal_file_id"]
    assert after["internal_content_version"] == before["internal_content_version"]  # 삭제만으로 버전 증가 금지
    assert after["checksum"] == before["checksum"]

    events = conn.execute(
        "SELECT event_type FROM processing_events WHERE drive_file_id = 'F1' ORDER BY event_id"
    ).fetchall()
    assert [e["event_type"] for e in events] == ["new", "deleted"]


# 휴지통 복원(Fix B) — 같은 프로젝트 폴더로 복원 -------------------------------------------------

def test_restore_into_same_project_folder_transitions_back_to_registered(conn, config):
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT, checksum="h1"

    fake.trash_file("F1")
    run_incremental(conn, config, fake)

    deleted = conn.execute(
        "SELECT internal_file_id, internal_content_version FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()

    fake.restore_file("F1")  # 같은 ROOT 폴더로, 내용 변경 없이 복원
    report = run_incremental(conn, config, fake)

    assert report["restored"] == ["0907회의록.pdf"]
    assert report["content_downloads"] == 0
    assert report["claude_api_calls"] == 0

    rows = conn.execute("SELECT * FROM files WHERE drive_file_id = 'F1'").fetchall()
    assert len(rows) == 1, "복원 시 registry row를 새로 만들면 안 된다"
    restored = rows[0]
    assert restored["is_deleted"] == 0
    assert restored["in_project_scope"] == 1
    assert restored["processing_status"] == "registered"
    assert restored["drive_file_id"] == "F1"
    assert restored["internal_file_id"] == deleted["internal_file_id"]  # 동일 row
    assert restored["internal_content_version"] == deleted["internal_content_version"]  # 단순 복원은 버전 불변
    assert restored["parent_folder_id"] == "ROOT"

    events = conn.execute(
        "SELECT event_type, details FROM processing_events WHERE drive_file_id = 'F1' ORDER BY event_id"
    ).fetchall()
    assert [e["event_type"] for e in events] == ["new", "deleted", "restored"]
    import json
    restored_details = json.loads(events[-1]["details"])
    assert restored_details["in_scope"] is True
    assert restored_details["filename"] == "0907회의록.pdf"


# 휴지통 복원(Fix B) — 프로젝트 scope 밖으로 복원 ---------------------------------------------

def test_restore_outside_project_scope_transitions_to_out_of_scope(conn, config):
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT

    fake.trash_file("F1")
    run_incremental(conn, config, fake)

    deleted = conn.execute(
        "SELECT internal_file_id, internal_content_version FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()

    fake.restore_file("F1", parents=["OUTSIDE"])  # scope 밖으로 복원
    report = run_incremental(conn, config, fake)

    assert report["restored"] == ["0907회의록.pdf"]

    rows = conn.execute("SELECT * FROM files WHERE drive_file_id = 'F1'").fetchall()
    assert len(rows) == 1, "중복 registry row가 생기면 안 된다"
    restored = rows[0]
    assert restored["is_deleted"] == 0
    assert restored["in_project_scope"] == 0
    assert restored["processing_status"] == "out_of_scope"
    assert restored["parent_folder_id"] == "OUTSIDE"  # 현재 실제 parent로 갱신
    assert restored["internal_content_version"] == deleted["internal_content_version"]

    events = conn.execute(
        "SELECT event_type, details FROM processing_events WHERE drive_file_id = 'F1' ORDER BY event_id"
    ).fetchall()
    assert [e["event_type"] for e in events] == ["new", "deleted", "restored"]
    import json
    assert json.loads(events[-1]["details"])["in_scope"] is False


# Google Workspace Native 파일 — 변경은 기록하되 내용은 조회하지 않음 -----------------------

def test_native_file_change_detected_without_content_check(conn, config):
    fake = FakeDriveClient()
    fake.add_file("D1", "기획안", "application/vnd.google-apps.document", parent="ROOT", checksum=None)
    run_bootstrap(conn, config, fake)

    fake.modify_file("D1", modifiedTime="t1")
    report = run_incremental(conn, config, fake)

    assert report["native_change_detected"] == ["기획안"]
    assert report["modified"] == []
    assert report["content_downloads"] == 0


# Test H: Checkpoint 실패 시 미전진 + 재시도 시 정상 복구 ---------------------------------

def test_checkpoint_not_advanced_on_failure_and_replay_succeeds(conn, config, monkeypatch):
    fake = _bootstrap_with_one_file(conn, config)
    before = conn.execute(
        "SELECT last_page_token FROM source_checkpoints WHERE project_id = ?", (config.project_id,)
    ).fetchone()["last_page_token"]

    fake.add_file("F2", "신규.pdf", "application/pdf", parent="ROOT", checksum="h2")

    import engine.drive.incremental as incremental_module
    original_log_event = incremental_module.log_event
    call_count = {"n": 0}

    def flaky_log_event(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated mid-processing failure")
        return original_log_event(*args, **kwargs)

    monkeypatch.setattr(incremental_module, "log_event", flaky_log_event)

    with pytest.raises(RuntimeError):
        run_incremental(conn, config, fake)

    after_fail = conn.execute(
        "SELECT last_page_token, last_run_status FROM source_checkpoints WHERE project_id = ?",
        (config.project_id,),
    ).fetchone()
    assert after_fail["last_page_token"] == before, "실패 시 page token이 전진하면 안 된다"
    assert after_fail["last_run_status"] == "failed"

    row = conn.execute("SELECT * FROM files WHERE drive_file_id = 'F2'").fetchone()
    assert row is None, "실패한 트랜잭션 안의 쓰기는 전부 롤백되어야 한다"

    monkeypatch.setattr(incremental_module, "log_event", original_log_event)
    report = run_incremental(conn, config, fake)

    assert report["result"] == "SUCCESS"
    assert report["new"] == ["신규.pdf"]

    after_success = conn.execute(
        "SELECT last_page_token, last_run_status FROM source_checkpoints WHERE project_id = ?",
        (config.project_id,),
    ).fetchone()
    assert after_success["last_page_token"] != before
    assert after_success["last_run_status"] == "success"


# checksum 미제공 — 다운로드해서 hash 계산하지 않고 명시적 상태로만 기록 -------------------------

def test_binary_checksum_unavailable_marks_unverified_without_download(conn, config):
    fake = FakeDriveClient()
    fake.add_file("F1", "scan.pdf", "application/pdf", parent="ROOT", checksum=None)
    run_bootstrap(conn, config, fake)

    fake.modify_file("F1", modifiedTime="t1")
    report = run_incremental(conn, config, fake)

    assert report["content_change_unverified"] == ["scan.pdf"]
    assert report["modified"] == []
    assert report["content_downloads"] == 0

    row = conn.execute(
        "SELECT processing_status, internal_content_version FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert row["processing_status"] == "content_change_unverified"
    assert row["internal_content_version"] == 1  # 확인되지 않은 변경이므로 버전은 올리지 않음


# Case1: 프로젝트 외부의 미등록 파일 변경 — 완전히 무시(Registry/Event 물질화 금지) --------------
# [정책 변경] 이전에는 report["out_of_scope"] == 1 을 기대하며 files/processing_events에
# out_of_scope row/event를 생성했다. Drive Changes API가 계정 전체 변경을 반환하므로,
# 프로젝트가 한 번도 관리한 적 없는 외부 파일(existing is None)을 매 실행마다 Registry/Event로
# 물질화하는 것은 아키텍처 원칙 위반이자 무한 증가의 원인이었다. 이제는 ignored_external
# 카운터만 올리고 아무것도 저장하지 않는다.

def test_external_file_change_never_scope_matched(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.add_file("EXT", "외부.pdf", "application/pdf", parent="OUTSIDE", checksum="hx")

    report = run_incremental(conn, config, fake)

    assert report["scope_matched"] == 0
    assert report["content_downloads"] == 0
    assert report["claude_api_calls"] == 0
    assert report["out_of_scope"] == 0
    assert report["ignored_external"] == 1

    assert conn.execute(
        "SELECT * FROM files WHERE drive_file_id = 'EXT'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT * FROM processing_events WHERE drive_file_id = 'EXT'"
    ).fetchall() == []


# Test A: unknown external file — Registry/Event 물질화 금지 -------------------------------

def test_unknown_external_file_change_is_ignored(conn, config):
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT (scope 안)
    fake.add_file("EXT", "unrelated.pdf", "application/pdf", parent="OUTSIDE", checksum="hx")

    report = run_incremental(conn, config, fake)

    assert report["out_of_scope"] == 0
    assert report["ignored_external"] == 1
    assert report["content_downloads"] == 0
    assert report["claude_api_calls"] == 0
    assert report["new"] == []

    assert conn.execute("SELECT * FROM files WHERE drive_file_id = 'EXT'").fetchone() is None
    assert conn.execute(
        "SELECT * FROM processing_events WHERE drive_file_id = 'EXT'"
    ).fetchall() == []


# Test B: external file repeated changes — 누적 없음 -------------------------------------

def test_external_file_repeated_changes_do_not_accumulate(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.add_file("EXT", "unrelated.pdf", "application/pdf", parent="OUTSIDE", checksum="hx")
    run_incremental(conn, config, fake)  # add_file change 소비 (이미 무시됨)

    events_baseline = conn.execute(
        "SELECT COUNT(*) c FROM processing_events WHERE project_id = ?", (config.project_id,)
    ).fetchone()["c"]
    files_baseline = conn.execute(
        "SELECT COUNT(*) c FROM files WHERE project_id = ?", (config.project_id,)
    ).fetchone()["c"]

    for i in range(3):
        fake.modify_file("EXT", modifiedTime=f"t{i}", md5Checksum=f"hx{i}")
        report = run_incremental(conn, config, fake)
        assert report["ignored_external"] == 1
        assert report["out_of_scope"] == 0

        assert conn.execute(
            "SELECT COUNT(*) c FROM processing_events WHERE project_id = ?", (config.project_id,)
        ).fetchone()["c"] == events_baseline
        assert conn.execute(
            "SELECT COUNT(*) c FROM files WHERE project_id = ?", (config.project_id,)
        ).fetchone()["c"] == files_baseline


# Test C: ignored external → project scope 진입 시 new project file로 정상 등록 ---------------

def test_ignored_external_file_later_enters_scope_registers_as_new(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.add_file("EXT", "unrelated.pdf", "application/pdf", parent="OUTSIDE", checksum="hx")

    run_incremental(conn, config, fake)  # run 1: 무시됨
    assert conn.execute("SELECT * FROM files WHERE drive_file_id = 'EXT'").fetchone() is None

    fake.modify_file("EXT", parents=["ROOT"])  # run 2: 프로젝트 root로 이동
    report = run_incremental(conn, config, fake)

    assert report["new"] == ["unrelated.pdf"]

    rows = conn.execute("SELECT * FROM files WHERE drive_file_id = 'EXT'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["processing_status"] == "registered"
    assert row["in_project_scope"] == 1
    assert row["is_deleted"] == 0

    events = conn.execute(
        "SELECT event_type FROM processing_events WHERE drive_file_id = 'EXT' ORDER BY event_id"
    ).fetchall()
    assert [e["event_type"] for e in events] == ["new"]  # 과거 out_of_scope 이벤트 없음


# Test D: managed project file → scope 밖 이동 — Case A는 그대로 유지되어야 한다 ---------------

def test_managed_file_move_out_of_scope_still_logs_out_of_scope(conn, config):
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT, scope=1
    fake.modify_file("F1", parents=["OUTSIDE"])

    report = run_incremental(conn, config, fake)

    assert report["out_of_scope"] == 1
    assert report["ignored_external"] == 0

    rows = conn.execute("SELECT * FROM files WHERE drive_file_id = 'F1'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["in_project_scope"] == 0
    assert row["processing_status"] == "out_of_scope"

    events = conn.execute(
        "SELECT event_type FROM processing_events WHERE drive_file_id = 'F1' ORDER BY event_id"
    ).fetchall()
    assert [e["event_type"] for e in events] == ["new", "out_of_scope"]


# Test E / Case5: excluded_folder_ids의 "미등록 신규 파일"도 external unknown과 동일하게 무시 -----
# [정책 변경] 이전에는 excluded 폴더의 신규 파일에 대해 files row(out_of_scope) + event를
# 생성했다. 정책 1에 따라 existing is None 이고 현재 scope 대상이 아니면(excluded 포함)
# 완전히 무시한다 — Registry/Event 생성 금지.

def test_excluded_folder_new_file_is_ignored(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.add_file("F2", "excluded.pdf", "application/pdf", parent="EXCLUDED", checksum="h2")

    report = run_incremental(conn, config, fake)

    assert report["out_of_scope"] == 0
    assert report["ignored_external"] == 1
    assert report["new"] == []

    assert conn.execute("SELECT * FROM files WHERE drive_file_id = 'F2'").fetchone() is None
    assert conn.execute(
        "SELECT * FROM processing_events WHERE drive_file_id = 'F2'"
    ).fetchall() == []


# Test E-2: 이미 관리 중이던 파일이 excluded 폴더로 이동 — Case A 상태 전이 기록 -----------------

def test_managed_file_moved_into_excluded_folder_logs_out_of_scope(conn, config):
    fake = _bootstrap_with_one_file(conn, config)  # F1: parent=ROOT, scope=1
    fake.modify_file("F1", parents=["EXCLUDED"])

    report = run_incremental(conn, config, fake)

    assert report["out_of_scope"] == 1
    assert report["ignored_external"] == 0

    row = conn.execute(
        "SELECT in_project_scope, processing_status FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert row["in_project_scope"] == 0
    assert row["processing_status"] == "out_of_scope"


# 폴더 자신의 rename — 하위 파일 내용 재처리 없음 --------------------------------------------

def test_folder_rename_does_not_trigger_content_reprocessing(conn, config):
    fake = FakeDriveClient()
    fake.add_folder("SUB1", "회의록", parent="ROOT")
    fake.add_file("F1", "a.pdf", "application/pdf", parent="SUB1", checksum="h1")
    run_bootstrap(conn, config, fake)

    fake.modify_file("SUB1", name="Meeting Notes")
    report = run_incremental(conn, config, fake)

    assert report["renamed"] == ["Meeting Notes"]
    folder_row = conn.execute(
        "SELECT folder_name, in_project_scope FROM folders WHERE folder_id = 'SUB1'"
    ).fetchone()
    assert folder_row["folder_name"] == "Meeting Notes"
    assert folder_row["in_project_scope"] == 1

    file_row = conn.execute(
        "SELECT internal_content_version FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert file_row["internal_content_version"] == 1


# 폴더 내부 → 내부 이동 — scope 유지, 하위 내용 재처리 없음 -----------------------------------

def test_folder_move_inside_to_inside_keeps_scope_true(conn, config):
    fake = FakeDriveClient()
    fake.add_folder("SUB1", "회의록", parent="ROOT")
    fake.add_folder("SUB2", "산출물", parent="ROOT")
    fake.add_file("F1", "a.pdf", "application/pdf", parent="SUB1", checksum="h1")
    run_bootstrap(conn, config, fake)

    fake.modify_file("SUB1", parents=["SUB2"])
    report = run_incremental(conn, config, fake)

    assert report["moved"] == ["회의록"]
    folder_row = conn.execute(
        "SELECT parent_folder_id, in_project_scope FROM folders WHERE folder_id = 'SUB1'"
    ).fetchone()
    assert folder_row["parent_folder_id"] == "SUB2"
    assert folder_row["in_project_scope"] == 1

    file_row = conn.execute(
        "SELECT internal_content_version, in_project_scope FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert file_row["internal_content_version"] == 1
    assert file_row["in_project_scope"] == 1


# 폴더 내부 → 외부 이동 — 하위 Registry 전체가 cascade로 out_of_scope --------------------------

def test_folder_move_inside_to_outside_cascades_out_of_scope(conn, config):
    fake = FakeDriveClient()
    fake.add_folder("SUB1", "회의록", parent="ROOT")
    fake.add_file("F1", "a.pdf", "application/pdf", parent="SUB1", checksum="h1")
    fake.add_file("F2", "b.pdf", "application/pdf", parent="SUB1", checksum="h2")
    run_bootstrap(conn, config, fake)

    fake.modify_file("SUB1", parents=["OUTSIDE"])
    report = run_incremental(conn, config, fake)

    assert report["out_of_scope"] == 2  # F1, F2 — SUB1 폴더 자신은 파일 카운트에 포함되지 않음

    folder_row = conn.execute(
        "SELECT in_project_scope FROM folders WHERE folder_id = 'SUB1'"
    ).fetchone()
    assert folder_row["in_project_scope"] == 0

    for fid in ("F1", "F2"):
        row = conn.execute(
            "SELECT in_project_scope, processing_status FROM files WHERE drive_file_id = ?", (fid,)
        ).fetchone()
        assert row["in_project_scope"] == 0
        assert row["processing_status"] == "out_of_scope"


# 폴더 외부 → 내부 이동 — 새로 편입된 subtree만 탐색, 기존 파일은 재처리되지 않음 -------------------

def test_folder_move_outside_to_inside_brings_subtree_without_full_rescan(conn, config):
    fake = FakeDriveClient()
    fake.add_file("F1", "existing.pdf", "application/pdf", parent="ROOT", checksum="h1")
    run_bootstrap(conn, config, fake)

    # 지금까지 전혀 알려지지 않은 프로젝트 외부의 폴더/파일
    fake.add_folder("EXT1", "외부폴더", parent="OUTSIDE_ROOT")
    fake.add_file("EXT_F1", "외부파일.pdf", "application/pdf", parent="EXT1", checksum="hx")
    run_incremental(conn, config, fake)  # scope 밖이므로 무시됨

    # EXT1을 프로젝트 root 하위로 이동
    fake.modify_file("EXT1", parents=["ROOT"])
    report = run_incremental(conn, config, fake)

    assert "외부파일.pdf" in report["new"]

    ext1_row = conn.execute(
        "SELECT in_project_scope FROM folders WHERE folder_id = 'EXT1'"
    ).fetchone()
    assert ext1_row["in_project_scope"] == 1

    extf1_row = conn.execute(
        "SELECT in_project_scope FROM files WHERE drive_file_id = 'EXT_F1'"
    ).fetchone()
    assert extf1_row["in_project_scope"] == 1

    # 기존에 이미 등록되어 있던 F1은 이번 편입 과정에서 전혀 건드려지지 않아야 한다
    # (프로젝트 전체 재스캔이 아니라 새로 편입된 subtree만 탐색했음을 증명)
    f1_events = conn.execute(
        "SELECT COUNT(*) c FROM processing_events WHERE drive_file_id = 'F1'"
    ).fetchone()["c"]
    assert f1_events == 1  # bootstrap 때 기록된 'new' 이벤트 하나뿐


# scope_config_version이 바뀌면 기존 scope 캐시를 신뢰하지 않고 재계산 -----------------------------

def test_scope_cache_invalidated_by_config_version_bump(conn, config):
    fake = FakeDriveClient()
    fake.add_folder("SUB1", "회의록", parent="ROOT")
    fake.add_file("F1", "a.pdf", "application/pdf", parent="SUB1", checksum="h1")
    run_bootstrap(conn, config, fake)

    cached = conn.execute(
        "SELECT in_project_scope, scope_config_version FROM folders WHERE folder_id = 'SUB1'"
    ).fetchone()
    assert cached["in_project_scope"] == 1
    assert cached["scope_config_version"] == 1

    # root_folder_id를 바꾸고 scope_config_version을 올린 새 설정 — SUB1은 더 이상 scope 안이 아니다.
    new_config = dataclasses.replace(
        config,
        drive=dataclasses.replace(config.drive, root_folder_id="OTHER_ROOT"),
        scope_config_version=2,
    )

    scope = ProjectScopeFilter(conn, new_config, fake)
    result = scope.resolve_folder_scope("SUB1")

    assert result is False  # 캐시(version 1, True)를 신뢰하지 않고 재계산됨
    updated = conn.execute(
        "SELECT in_project_scope, scope_config_version FROM folders WHERE folder_id = 'SUB1'"
    ).fetchone()
    assert updated["scope_config_version"] == 2
    assert updated["in_project_scope"] == 0

    # 재계산은 scope 판정(metadata 조회)만 수행 — 파일 내용은 전혀 건드리지 않음
    file_row = conn.execute(
        "SELECT internal_content_version, checksum FROM files WHERE drive_file_id = 'F1'"
    ).fetchone()
    assert file_row["internal_content_version"] == 1
    assert file_row["checksum"] == "h1"


# 다중 페이지 changes.list — 모든 페이지 처리 + 최종 newStartPageToken만 checkpoint에 반영 -------------

def test_multi_page_changes_processed_and_checkpoint_uses_final_token(conn, config):
    fake = FakeDriveClient(page_size=2)
    fake.add_file("F1", "a.pdf", "application/pdf", parent="ROOT", checksum="h1")
    run_bootstrap(conn, config, fake)

    for i in range(2, 6):  # F2~F5: 4개의 change, page_size=2 → 2페이지로 분할되어 반환됨
        fake.add_file(f"F{i}", f"file{i}.pdf", "application/pdf", parent="ROOT", checksum=f"h{i}")

    report = run_incremental(conn, config, fake)

    assert report["changes_received"] == 4
    assert sorted(report["new"]) == sorted(f"file{i}.pdf" for i in range(2, 6))
    assert report["result"] == "SUCCESS"

    checkpoint = conn.execute(
        "SELECT last_page_token FROM source_checkpoints WHERE project_id = ?", (config.project_id,)
    ).fetchone()
    assert checkpoint["last_page_token"] == fake.get_start_page_token()


# Checkpoint 원자성: F1 성공, F2 성공, F3 실패 → 전부 롤백, 재시도 시 중복 없이 반영 -------------------

def test_partial_batch_failure_no_duplicate_registry_or_events_on_replay(conn, config, monkeypatch):
    fake = _bootstrap_with_one_file(conn, config)  # F1 기존 등록

    events_after_bootstrap = conn.execute(
        "SELECT COUNT(*) c FROM processing_events WHERE project_id = ?", (config.project_id,)
    ).fetchone()["c"]
    before_token = conn.execute(
        "SELECT last_page_token FROM source_checkpoints WHERE project_id = ?", (config.project_id,)
    ).fetchone()["last_page_token"]

    fake.modify_file("F1", md5Checksum="h1-v2")  # 1) 성공할 변경
    fake.add_file("F2", "new2.pdf", "application/pdf", parent="ROOT", checksum="h2")  # 2) 성공할 변경
    fake.add_file("F3", "new3.pdf", "application/pdf", parent="ROOT", checksum="h3")  # 3) 실패를 유발

    import engine.drive.incremental as incremental_module
    original_log_event = incremental_module.log_event
    call_count = {"n": 0}

    def flaky_log_event(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated failure on third change")
        return original_log_event(*args, **kwargs)

    monkeypatch.setattr(incremental_module, "log_event", flaky_log_event)

    with pytest.raises(RuntimeError):
        run_incremental(conn, config, fake)

    f1 = conn.execute("SELECT internal_content_version FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert f1["internal_content_version"] == 1  # F1 UPDATE도 통째로 롤백됨
    assert conn.execute("SELECT * FROM files WHERE drive_file_id = 'F2'").fetchone() is None
    assert conn.execute("SELECT * FROM files WHERE drive_file_id = 'F3'").fetchone() is None

    events_after_failure = conn.execute(
        "SELECT COUNT(*) c FROM processing_events WHERE project_id = ?", (config.project_id,)
    ).fetchone()["c"]
    assert events_after_failure == events_after_bootstrap

    checkpoint_after_fail = conn.execute(
        "SELECT last_page_token FROM source_checkpoints WHERE project_id = ?", (config.project_id,)
    ).fetchone()["last_page_token"]
    assert checkpoint_after_fail == before_token

    # 재시도: 동일 변경을 다시 받아도 중복 생성 없이 정확히 반영되어야 한다
    monkeypatch.setattr(incremental_module, "log_event", original_log_event)
    report = run_incremental(conn, config, fake)

    assert report["result"] == "SUCCESS"

    for fid in ("F1", "F2", "F3"):
        count = conn.execute(
            "SELECT COUNT(*) c FROM files WHERE drive_file_id = ?", (fid,)
        ).fetchone()["c"]
        assert count == 1, f"{fid}는 UPSERT로 정확히 1건이어야 한다 (중복 없음)"

    f1_final = conn.execute("SELECT internal_content_version FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert f1_final["internal_content_version"] == 2

    events_after_success = conn.execute(
        "SELECT COUNT(*) c FROM processing_events WHERE project_id = ?", (config.project_id,)
    ).fetchone()["c"]
    assert events_after_success == events_after_bootstrap + 3  # F1 modified + F2 new + F3 new, 중복 없음
