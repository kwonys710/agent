import pytest

from engine.drive.bootstrap import run_bootstrap
from engine.drive.incremental import BootstrapRequired, run_incremental
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


# 삭제 --------------------------------------------------------------------------------

def test_deleted_file_marks_registry_without_removing_row(conn, config):
    fake = _bootstrap_with_one_file(conn, config)
    fake.trash_file("F1")

    report = run_incremental(conn, config, fake)

    assert report["deleted"] == ["0907회의록.pdf"]
    row = conn.execute("SELECT is_deleted FROM files WHERE drive_file_id = 'F1'").fetchone()
    assert row["is_deleted"] == 1


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
