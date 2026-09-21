"""Milestone 1 End-to-End 테스트.

CSV Import → 분석 → Score → 댓글 3개 → Action Queue → Dry Run → SQLite 기록 → Summary
전체가 실제로 동작하는지 확인한다(외부 API 호출 없음).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from targeting_agent.core.database import get_connection, init_db
from targeting_agent.core.models import ActionStatus, MediaStatus
from targeting_agent.main import main
from targeting_agent.pipeline import TargetingPipeline, format_summary, next_run_id

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CSV = PACKAGE_ROOT / "samples" / "candidates_sample.csv"


def _tmp_config(tmp_path: Path, **overrides) -> Path:
    """실제 config.yaml을 복제하되 경로만 임시 디렉터리로 돌린 설정 파일."""
    raw = yaml.safe_load((PACKAGE_ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["paths"] = {
        "data_dir": str(tmp_path / "data"),
        "database": str(tmp_path / "data" / "targeting.db"),
        "log_dir": str(tmp_path / "data" / "logs"),
        "export_dir": str(tmp_path / "data" / "exports"),
        "cache_dir": str(tmp_path / "data" / "cache"),
    }
    raw["profile"] = {"path": str(PACKAGE_ROOT / "profiles" / "dailyreels.yaml")}
    # 단위/E2E 테스트는 실제 Claude CLI를 호출하지 않는다(Phase 13 원칙).
    raw["ai"] = {**raw.get("ai", {}), "provider": "heuristic"}
    raw["discovery"] = {**raw["discovery"], "import": {"path": str(SAMPLE_CSV)}}
    for key, value in overrides.items():
        raw[key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _load_config(path: Path):
    from targeting_agent.core.config import load_config

    return load_config(path, load_env=False)


def test_full_dry_run_pipeline(tmp_path) -> None:
    config = _load_config(_tmp_config(tmp_path))
    conn = get_connection(config.db_path)
    init_db(conn)
    try:
        pipeline = TargetingPipeline(config, conn, run_id=next_run_id(conn), seed=11)
        summary = pipeline.run()

        # Discovery: 샘플 10건 중 광고/비공개/영어/초대형 계정은 걸러진다
        assert summary.discovery.found == 10
        assert summary.discovery.filtered == 4
        assert summary.discovery.new == 6

        # 분석/Score
        assert summary.analysis.analyzed == 6
        assert summary.analysis.above_minimum >= 1
        assert summary.analysis.errors == 0

        # 댓글: 대상 게시물마다 후보 3개
        per_post = int(config.get("comments.candidates_per_post"))
        assert summary.comments.posts >= 1
        assert summary.comments.generated <= summary.comments.posts * per_post
        # 중복 필터 때문에 일부 게시물은 3개 미만일 수 있으나, 게시물당 최소 2개는 확보한다
        assert summary.comments.generated >= summary.comments.posts * (per_post - 1)
        assert summary.comments.none_generated == 0

        # Action Queue는 만들어지되, 승인 전에는 실행되지 않는다(Phase 14.1 Approval Gate)
        assert summary.queue.likes >= 1 and summary.queue.comments >= 1
        assert summary.execution.success == 0
        assert summary.execution.failed == 0
        assert summary.execution.awaiting_approval == summary.queue.total
        assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0
        assert summary.dry_run is True

        # 운영자가 Dashboard에서 승인한 뒤에야 실행 대상이 된다
        from targeting_agent.actions.controller import ActionController
        from targeting_agent.actions.executors import create_executor
        from targeting_agent.core.models import ActionType
        from targeting_agent.dashboard.service import DashboardService

        media_pk = int(
            conn.execute(
                "SELECT media_pk FROM action_queue WHERE action_type = 'LIKE' LIMIT 1"
            ).fetchone()[0]
        )
        DashboardService(conn, config).approve(media_pk, [ActionType.LIKE])

        executor = create_executor(
            config.executor_mode, export_dir=config.export_dir, run_id="approved", dry_run=True
        )
        execution = ActionController(config, conn, executor).run()

        assert execution.success == 1
        assert conn.execute("SELECT COUNT(*) FROM interactions WHERE dry_run = 1").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM interactions WHERE dry_run = 0").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM action_queue WHERE status = ?", (ActionStatus.SUCCESS.value,)
        ).fetchone()[0] == 1

        export = Path(str(executor.health_check()["export_path"]))
        assert export.exists() and export.stat().st_size > 0

        text = format_summary(summary, config)
        assert "DailyReels Targeting Agent" in text and "Daily Limit" in text
    finally:
        conn.close()


def test_rerun_is_idempotent(tmp_path) -> None:
    """같은 후보로 다시 실행해도 중복 수집/중복 Action/중복 Interaction이 생기지 않는다."""
    config_path = _tmp_config(tmp_path)
    config = _load_config(config_path)

    conn = get_connection(config.db_path)
    init_db(conn)
    try:
        first = TargetingPipeline(config, conn, run_id=next_run_id(conn), seed=11).run()
        counts_before = _counts(conn)

        second = TargetingPipeline(config, conn, run_id=next_run_id(conn), seed=11).run()
        counts_after = _counts(conn)

        assert second.discovery.new == 0
        # 필터를 통과해 DB에 저장됐던 건들이 이번엔 전부 중복으로 잡힌다
        assert second.discovery.duplicate == first.discovery.new
        assert counts_after["candidate_media"] == counts_before["candidate_media"]
        # 승인 전이므로 실행은 일어나지 않고, 중복 생성도 없다
        assert counts_after["action_queue"] == counts_before["action_queue"]
        assert counts_after["interactions"] == counts_before["interactions"] == 0
    finally:
        conn.close()


def test_main_cli_dry_run(tmp_path, capsys) -> None:
    config_path = _tmp_config(tmp_path)
    exit_code = main(["--config", str(config_path), "--import", str(SAMPLE_CSV), "--seed", "5"])
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert "DailyReels Targeting Agent" in captured
    assert "Discovery" in captured and "Action Queue" in captured

    db = tmp_path / "data" / "targeting.db"
    assert db.exists()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM candidate_media").fetchone()[0] > 0
        statuses = {row[0] for row in conn.execute("SELECT DISTINCT status FROM candidate_media")}
        assert statuses <= {s.value for s in MediaStatus}
    finally:
        conn.close()


def test_main_cli_stats(tmp_path, capsys) -> None:
    config_path = _tmp_config(tmp_path)
    assert main(["--config", str(config_path), "--stats"]) == 0
    assert "오늘 통계" in capsys.readouterr().out


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("candidate_media", "action_queue", "interactions", "comment_drafts")
    }
