from engine.drive.bootstrap import BootstrapAlreadyDone, run_bootstrap
from tests.fakes import FakeDriveClient


def test_bootstrap_registers_files_and_folders_without_downloading_content(conn, config):
    fake = FakeDriveClient()
    fake.add_folder("SUB1", "회의록", parent="ROOT")
    fake.add_file("F1", "0907회의록.pdf", "application/pdf", parent="SUB1", checksum="h1")
    fake.add_file(
        "F2", "검색대상정의서.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        parent="ROOT", checksum="h2",
    )

    result = run_bootstrap(conn, config, fake)

    assert result["files_registered"] == 2
    assert result["folders_visited"] == 1
    assert result["start_page_token"] is not None

    rows = conn.execute(
        "SELECT drive_file_id, in_project_scope FROM files WHERE project_id = ?",
        (config.project_id,),
    ).fetchall()
    assert {row["drive_file_id"] for row in rows} == {"F1", "F2"}
    assert all(row["in_project_scope"] == 1 for row in rows)

    checkpoint = conn.execute(
        "SELECT last_page_token, last_run_status FROM source_checkpoints WHERE project_id = ?",
        (config.project_id,),
    ).fetchone()
    assert checkpoint["last_page_token"] is not None
    assert checkpoint["last_run_status"] == "success"

    # Bootstrap 단계에서는 파일 내용을 조회하는 메서드 자체가 클라이언트에 존재하지 않는다.
    assert not hasattr(fake, "download_file")
    assert not hasattr(fake, "get_file_content")


def test_bootstrap_excludes_folders_outside_root(conn, config):
    fake = FakeDriveClient()
    fake.add_file("F1", "in_scope.pdf", "application/pdf", parent="ROOT", checksum="h1")
    # OUTSIDE는 ROOT 하위가 아니므로 애초에 walk 대상에 포함되지 않는다.
    fake.add_file("F2", "outside.pdf", "application/pdf", parent="OUTSIDE", checksum="h2")

    result = run_bootstrap(conn, config, fake)

    assert result["files_registered"] == 1
    row = conn.execute(
        "SELECT drive_file_id FROM files WHERE project_id = ?", (config.project_id,)
    ).fetchall()
    assert {r["drive_file_id"] for r in row} == {"F1"}


def test_bootstrap_refuses_rerun_without_override(conn, config):
    fake = FakeDriveClient()
    fake.add_file("F1", "a.pdf", "application/pdf", parent="ROOT", checksum="h1")
    run_bootstrap(conn, config, fake)

    try:
        run_bootstrap(conn, config, fake)
        assert False, "override 없이 재실행이 허용되면 안 된다"
    except BootstrapAlreadyDone:
        pass


def test_bootstrap_override_allows_rerun(conn, config):
    fake = FakeDriveClient()
    fake.add_file("F1", "a.pdf", "application/pdf", parent="ROOT", checksum="h1")
    run_bootstrap(conn, config, fake)

    result = run_bootstrap(conn, config, fake, override=True)
    assert result["files_registered"] == 1
