"""Incremental Mode: 매일 자동 실행.

핵심 원칙(Architecture v3.1):
1. 전체 파일 목록을 다시 조회하지 않는다 — 저장된 last_page_token으로
   changes.list()를 호출해 "변경된 file_id"만 받는다.
2. "변경 발견"과 "내용 분석 필요 여부 판정"은 분리한다 — 이 모듈은 판정까지만 하고,
   실제 파일 다운로드나 Claude 호출은 이번 MVP 범위에 아예 존재하지 않는다.
3. Checkpoint(page token)는 이번 실행의 모든 DB 반영이 성공한 뒤에만 전진한다.
   중간에 예외가 나면 트랜잭션 전체가 롤백되어, 다음 실행이 같은 구간을 다시 받는다.
4. 폴더가 scope 경계를 넘나드는 이동은 별도 처리한다(engine.drive.subtree) —
   내부→외부는 이미 알려진 하위만 갱신, 외부→내부는 새로 편입된 하위만 조회한다.
"""
from __future__ import annotations

from .client import DriveClient, raw_to_meta
from .models import FileCategory
from .registry import log_event, now, upsert_file
from .scope import ProjectScopeFilter
from .subtree import bring_subtree_into_scope, cascade_mark_out_of_scope
from engine.state.database import transaction


class BootstrapRequired(RuntimeError):
    pass


def _empty_report() -> dict:
    return {
        "changes_received": 0,
        "scope_matched": 0,
        "new": [],
        "modified": [],
        "renamed": [],
        "moved": [],
        "deleted": [],
        "out_of_scope": 0,
        "metadata_only": 0,
        "content_change_unverified": [],
        "native_change_detected": [],
        "content_downloads": 0,
        "claude_api_calls": 0,
    }


def run_incremental(conn, config, drive_client: DriveClient) -> dict:
    checkpoint = conn.execute(
        "SELECT * FROM source_checkpoints WHERE project_id = ? AND source_type = 'google_drive'",
        (config.project_id,),
    ).fetchone()

    if checkpoint is None or not checkpoint["last_page_token"]:
        raise BootstrapRequired(
            f"프로젝트 '{config.project_id}'의 Drive checkpoint가 없습니다. "
            f"먼저 'python scripts/drive_bootstrap.py --project {config.project_id}'를 실행하세요."
        )

    last_page_token = checkpoint["last_page_token"]
    report = _empty_report()

    try:
        changes, new_start_page_token, _ = drive_client.list_changes(last_page_token)
        report["changes_received"] = len(changes)

        with transaction(conn):
            scope = ProjectScopeFilter(conn, config, drive_client)

            for change in changes:
                _process_change(conn, config, drive_client, scope, change, report)

            conn.execute(
                """
                UPDATE source_checkpoints
                SET last_page_token = ?, pending_page_token = NULL,
                    last_successful_sync_at = ?, last_run_status = 'success', last_error = NULL,
                    updated_at = ?
                WHERE project_id = ? AND source_type = 'google_drive'
                """,
                (new_start_page_token or last_page_token, now(), now(), config.project_id),
            )

        report["result"] = "SUCCESS"
        return report

    except Exception as exc:
        # 트랜잭션은 이미 롤백된 상태 — last_page_token은 건드리지 않고 실패 상태만 별도로 기록한다.
        conn.execute(
            """
            UPDATE source_checkpoints
            SET last_run_status = 'failed', last_error = ?, updated_at = ?
            WHERE project_id = ? AND source_type = 'google_drive'
            """,
            (str(exc), now(), config.project_id),
        )
        conn.commit()
        raise


def _process_change(conn, config, drive_client, scope: ProjectScopeFilter, change: dict, report: dict) -> None:
    drive_file_id = change.get("fileId")
    if not drive_file_id:
        return

    raw_file = change.get("file")
    removed = bool(change.get("removed")) or bool(raw_file and raw_file.get("trashed"))

    existing = conn.execute(
        "SELECT * FROM files WHERE project_id = ? AND drive_file_id = ?",
        (config.project_id, drive_file_id),
    ).fetchone()

    if removed:
        if existing is not None:
            conn.execute(
                """
                UPDATE files SET is_deleted = 1, processing_status = 'deleted',
                                 last_seen_at = ?, updated_at = ?
                WHERE project_id = ? AND drive_file_id = ?
                """,
                (now(), now(), config.project_id, drive_file_id),
            )
            log_event(conn, config, drive_file_id, "deleted", {"filename": existing["filename"]})
            report["deleted"].append(existing["filename"])
        return

    if raw_file is None:
        return

    meta = raw_to_meta(raw_file)

    if meta.category == FileCategory.FOLDER:
        _process_folder_change(conn, config, drive_client, scope, meta, report)
        return

    in_scope = scope.resolve_folder_scope(meta.parent_folder_id)

    if not in_scope:
        if existing is not None:
            # parent_folder_id도 현재 값으로 갱신해야 한다 — 갱신하지 않으면, 나중에 같은 파일이
            # (심지어 원래 있던 바로 그 폴더로) 다시 scope 안으로 돌아왔을 때 저장된 parent가
            # 여전히 예전 in-scope 폴더를 가리키고 있어 diff(reasons)가 비어 재진입 감지 자체를
            # 놓치는 버그가 발생한다 (실제 Drive 통합 테스트에서 발견).
            conn.execute(
                """
                UPDATE files SET in_project_scope = 0, processing_status = 'out_of_scope',
                                 parent_folder_id = ?, last_seen_at = ?, updated_at = ?
                WHERE project_id = ? AND drive_file_id = ?
                """,
                (meta.parent_folder_id, now(), now(), config.project_id, drive_file_id),
            )
        else:
            upsert_file(conn, config, meta, in_scope=False, status="out_of_scope")
        log_event(conn, config, drive_file_id, "out_of_scope", {"filename": meta.name})
        report["out_of_scope"] += 1
        return

    report["scope_matched"] += 1

    if existing is None:
        upsert_file(conn, config, meta, in_scope=True, status="registered")
        log_event(conn, config, drive_file_id, "new", {"filename": meta.name})
        report["new"].append(meta.name)
        return

    if existing["is_deleted"]:
        # 휴지통에서 복원된 경우: scope 재진입 버그와 동일한 계열의 문제 —
        # is_deleted/processing_status가 'deleted'로 계속 남아있으면 안 된다.
        # (이후 rename/modify 판정은 아래 로직이 그대로 이어서 처리한다)
        conn.execute(
            """
            UPDATE files
            SET is_deleted = 0,
                processing_status = CASE WHEN processing_status = 'deleted'
                                          THEN 'registered' ELSE processing_status END,
                last_seen_at = ?, updated_at = ?
            WHERE project_id = ? AND drive_file_id = ?
            """,
            (now(), now(), config.project_id, drive_file_id),
        )

    reasons = []
    if existing["filename"] != meta.name:
        reasons.append("renamed")
    if existing["parent_folder_id"] != meta.parent_folder_id:
        reasons.append("moved")

    if meta.category == FileCategory.BINARY:
        if meta.md5_checksum is None:
            # checksum을 제공받지 못한 경우: 이번 MVP는 다운로드해서 hash를 계산하지 않는다
            # (의도적 제한 — Content Processing 단계의 몫). "확인된 무변경"과 뒤섞이지 않도록
            # 명시적으로 별도 상태로만 기록한다.
            if reasons:
                _apply_rename_move(conn, config, existing, meta, reasons, report)
                return
            conn.execute(
                """
                UPDATE files
                SET modified_time = ?, processing_status = 'content_change_unverified',
                    last_seen_at = ?, updated_at = ?
                WHERE project_id = ? AND drive_file_id = ?
                """,
                (meta.modified_time, now(), now(), config.project_id, drive_file_id),
            )
            log_event(conn, config, drive_file_id, "content_change_unverified", {"filename": meta.name})
            report["content_change_unverified"].append(meta.name)
            return

        content_changed = (existing["checksum"] or None) != meta.md5_checksum
        if content_changed:
            new_version = existing["internal_content_version"] + 1
            conn.execute(
                """
                UPDATE files
                SET filename = ?, parent_folder_id = ?, size = ?, modified_time = ?,
                    checksum = ?, drive_version = ?, processing_status = 'modified',
                    internal_content_version = ?, in_project_scope = 1,
                    last_seen_at = ?, updated_at = ?
                WHERE project_id = ? AND drive_file_id = ?
                """,
                (
                    meta.name, meta.parent_folder_id, meta.size, meta.modified_time,
                    meta.md5_checksum, meta.head_revision_id, new_version,
                    now(), now(), config.project_id, drive_file_id,
                ),
            )
            log_event(
                conn, config, drive_file_id, "modified",
                {"reasons": reasons, "internal_content_version": new_version},
            )
            report["modified"].append(meta.name)
            return

    if reasons:
        _apply_rename_move(conn, config, existing, meta, reasons, report)
        return

    if meta.category == FileCategory.GOOGLE_NATIVE and existing["modified_time"] != meta.modified_time:
        conn.execute(
            "UPDATE files SET modified_time = ?, last_seen_at = ?, updated_at = ? "
            "WHERE project_id = ? AND drive_file_id = ?",
            (meta.modified_time, now(), now(), config.project_id, drive_file_id),
        )
        log_event(conn, config, drive_file_id, "native_change_detected", {"filename": meta.name})
        report["native_change_detected"].append(meta.name)
        return

    # 그 외: 실질적 변경 없음 (권한/조회시각 등 metadata만 흔들린 경우)
    conn.execute(
        "UPDATE files SET last_seen_at = ?, updated_at = ? WHERE project_id = ? AND drive_file_id = ?",
        (now(), now(), config.project_id, drive_file_id),
    )
    log_event(conn, config, drive_file_id, "metadata_only", {})
    report["metadata_only"] += 1


def _apply_rename_move(conn, config, existing, meta, reasons: list, report: dict) -> None:
    # scope 밖에 있다가 돌아온 파일은 lifecycle 상태를 명시적으로 'registered'로 복구한다
    # (engine.drive.subtree.bring_subtree_into_scope가 폴더 재진입 시 이미 쓰는 것과 동일한 규칙).
    # 그 외의 평범한 scope 내부 rename/move는 기존 processing_status(modified 등)를 그대로 둔다.
    conn.execute(
        """
        UPDATE files
        SET filename = ?, parent_folder_id = ?, modified_time = ?,
            in_project_scope = 1,
            processing_status = CASE WHEN processing_status = 'out_of_scope'
                                      THEN 'registered' ELSE processing_status END,
            last_seen_at = ?, updated_at = ?
        WHERE project_id = ? AND drive_file_id = ?
        """,
        (meta.name, meta.parent_folder_id, meta.modified_time, now(), now(), config.project_id, meta.drive_file_id),
    )
    if "renamed" in reasons:
        log_event(conn, config, meta.drive_file_id, "renamed",
                   {"old_name": existing["filename"], "new_name": meta.name})
        report["renamed"].append(meta.name)
    if "moved" in reasons:
        log_event(conn, config, meta.drive_file_id, "moved",
                   {"old_parent": existing["parent_folder_id"], "new_parent": meta.parent_folder_id})
        report["moved"].append(meta.name)


def _process_folder_change(conn, config, drive_client, scope: ProjectScopeFilter, meta, report: dict) -> None:
    """폴더 자신의 변경 이벤트 처리.

    폴더 자신의 scope 캐시는 신뢰하지 않고 강제로 재계산한다(force_resolve_folder_scope) —
    자신의 parent가 방금 바뀌었을 수 있기 때문이다. rename/이동은 하위 파일 내용을
    재분석하지 않으며, scope 경계를 넘는 이동만 subtree 갱신을 유발한다(engine.drive.subtree).
    """
    existing = conn.execute(
        "SELECT * FROM folders WHERE project_id = ? AND folder_id = ?",
        (config.project_id, meta.drive_file_id),
    ).fetchone()

    new_scope = scope.force_resolve_folder_scope(meta.drive_file_id, meta.parent_folder_id, meta.name)

    if existing is None:
        if new_scope:
            log_event(conn, config, meta.drive_file_id, "new", {"filename": meta.name, "kind": "folder"})
            bring_subtree_into_scope(conn, config, drive_client, scope, meta.drive_file_id, report)
        return

    old_scope = bool(existing["in_project_scope"])
    old_name = existing["folder_name"]
    old_parent = existing["parent_folder_id"]

    if old_scope and new_scope:
        if old_name != meta.name:
            log_event(conn, config, meta.drive_file_id, "renamed",
                       {"old_name": old_name, "new_name": meta.name, "kind": "folder"})
            report["renamed"].append(meta.name)
        if old_parent != meta.parent_folder_id:
            log_event(conn, config, meta.drive_file_id, "moved",
                       {"old_parent": old_parent, "new_parent": meta.parent_folder_id, "kind": "folder"})
            report["moved"].append(meta.name)
        return

    if old_scope and not new_scope:
        log_event(conn, config, meta.drive_file_id, "out_of_scope", {"filename": meta.name, "kind": "folder"})
        cascade_mark_out_of_scope(conn, config, meta.drive_file_id, report)
        return

    if not old_scope and new_scope:
        log_event(conn, config, meta.drive_file_id, "new",
                   {"filename": meta.name, "kind": "folder", "reason": "moved_into_scope"})
        bring_subtree_into_scope(conn, config, drive_client, scope, meta.drive_file_id, report)
        return

    # not old_scope and not new_scope: 관심 밖 폴더 — 아무 것도 하지 않음
