"""Phase 15 — Scheduled Run / Daily Summary 테스트.

실제 Claude CLI·Instagram·Windows Task Scheduler를 건드리지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from targeting_agent.ai.intelligence import DAILY_METRIC, ClaudeIntelligence
from targeting_agent.ai.schema import FailureReason, TargetingAnalysis
from targeting_agent.ai.claude_runner import RunnerResult
from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    bump_stat,
    enqueue_action,
    insert_candidate,
    today_str,
    update_candidate_status,
    utc_now,
)
from targeting_agent.core.models import ActionType, FeedbackType, MediaStatus, RawCandidate
from targeting_agent.pipeline import CandidateProcessor
from targeting_agent.scheduler.lock import SchedulerLock
from targeting_agent.scheduler.runner import STATUS_LOCKED, STATUS_OK, run_scheduled
from targeting_agent.scheduler.summary import (
    build_summary_data,
    cleanup_old_reports,
    render_summary_html,
)

PAYLOAD = {
    "summary": "퇴근 후 저녁",
    "primary_topic": "퇴근",
    "topics": ["퇴근"],
    "mood": "편안",
    "language": "ko",
    "relevance_score": 0.85,
    "relevance_reason": "직장인 맥락",
    "comment_candidates": ["퇴근하고 먹는 저녁이 최고죠"],
}


class FakeRunner:
    def __init__(self, result: RunnerResult | None = None, available: bool = True):
        self.result = result or RunnerResult(
            ok=True, analysis=TargetingAnalysis.model_validate(PAYLOAD), cost_usd=0.02
        )
        self.calls: list[str] = []
        self._available = available

    def available(self) -> bool:
        return self._available

    def run(self, prompt: str) -> RunnerResult:
        self.calls.append(prompt)
        return self.result


def _config(config: Config, tmp_path: Path, **scheduler_overrides) -> Config:
    """임시 경로와 claude_code provider를 쓰는 설정."""
    scheduler = {
        "enabled": True,
        "process_inbox": True,
        "process_new_candidates": True,
        "max_candidates_per_run": 10,
        "lock": {"enabled": True, "path": str(tmp_path / ".scheduler.lock"), "stale_minutes": 60},
        "summary": {"enabled": True, "output_dir": str(tmp_path / "reports"), "keep_days": 30},
        **scheduler_overrides,
    }
    discovery = {
        **config.raw["discovery"],
        "inbox": {
            "enabled": True,
            "path": str(tmp_path / "inbox"),
            "processed_dir": str(tmp_path / "inbox" / "processed"),
            "failed_dir": str(tmp_path / "inbox" / "failed"),
        },
    }
    ai = {**config.raw.get("ai", {}), "provider": "claude_code", "daily_request_limit": 20}
    return Config(
        raw={**config.raw, "scheduler": scheduler, "discovery": discovery, "ai": ai},
        path=config.path,
        base_dir=config.base_dir,
    )


def _factory(runner: FakeRunner):
    def build(config: Config, conn):
        from targeting_agent.analysis.profile_analyzer import load_profile

        profile = load_profile(config.profile_path, conn)
        return CandidateProcessor(
            config,
            conn,
            profile=profile,
            intelligence=ClaudeIntelligence(config, profile, runner=runner),
            seed=1,
        )

    return build


def _candidate(conn, media_id="M1", caption="퇴근 후 저녁 #퇴근 #직장인", username="creator_a") -> int:
    media_pk = insert_candidate(
        conn,
        RawCandidate(
            media_id=media_id,
            permalink=f"https://www.instagram.com/reel/{media_id}/",
            username=username,
            caption=caption,
            hashtags=["퇴근"] if caption else [],
            followers=5000,
        ),
        status=MediaStatus.NEW if caption else MediaStatus.NEEDS_ENRICHMENT,
    )
    conn.commit()
    return int(media_pk)


def _run(conn, config, runner: FakeRunner | None = None, **kwargs):
    runner = runner or FakeRunner()
    return run_scheduled(config, conn, processor_factory=_factory(runner), **kwargs), runner


# --- 1~3. 기본 실행 ---------------------------------------------------------
def test_scheduled_run_starts_and_writes_summary(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)
    result, _ = _run(conn, cfg)

    assert result.status == STATUS_OK and result.exit_code == 0
    summary = Path(result.summary_path)
    assert summary.exists() and summary.name.startswith("daily_summary_")
    assert (summary.parent / "latest.html").exists()


def test_scheduled_run_processes_inbox(conn, config, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.csv").write_text(
        "url,username,caption\n"
        "https://www.instagram.com/reel/AAA/,creator_a,퇴근 후 저녁 #퇴근\n",
        encoding="utf-8",
    )
    cfg = _config(config, tmp_path)
    result, runner = _run(conn, cfg)

    assert result.ingest and result.ingest.added == 1
    assert result.analyzed == 1 and len(runner.calls) == 1
    assert (inbox / "processed" / "a.csv").exists()


def test_new_candidate_analyzed(conn, config, tmp_path) -> None:
    _candidate(conn)
    result, runner = _run(conn, _config(config, tmp_path))

    assert (result.processed, result.analyzed) == (1, 1)
    assert len(runner.calls) == 1
    assert conn.execute("SELECT status FROM candidate_media").fetchone()[0] == MediaStatus.SCORED.value


# --- 4~7. Claude 호출 가드 ---------------------------------------------------
def test_already_analyzed_candidate_is_not_resent(conn, config, tmp_path) -> None:
    _candidate(conn)
    cfg = _config(config, tmp_path)
    _, runner = _run(conn, cfg)
    second, runner2 = _run(conn, cfg)

    assert len(runner.calls) == 1
    assert second.processed == 0 and runner2.calls == []   # SCORED는 대상이 아니다


def test_needs_enrichment_never_calls_claude(conn, config, tmp_path) -> None:
    _candidate(conn, "URLONLY", caption="")
    result, runner = _run(conn, _config(config, tmp_path))

    assert runner.calls == []
    assert result.analyzed == 0
    assert conn.execute("SELECT status FROM candidate_media").fetchone()[0] == (
        MediaStatus.NEEDS_ENRICHMENT.value
    )


def test_cache_hit_avoids_second_call(conn, config, tmp_path) -> None:
    """같은 내용의 다른 후보는 캐시를 재사용한다."""
    _candidate(conn, "M1")
    cfg = _config(config, tmp_path)
    _, runner = _run(conn, cfg)

    _candidate(conn, "M2")
    result2, runner2 = _run(conn, cfg, runner=runner)
    assert result2.analyzed == 1
    assert len(runner.calls) == 1 and result2.cache_hits == 1


def test_no_new_candidates_means_no_claude(conn, config, tmp_path) -> None:
    result, runner = _run(conn, _config(config, tmp_path))
    assert result.processed == 0 and runner.calls == []


# --- 8~10. 한도 / fallback ---------------------------------------------------
def test_per_run_candidate_limit(conn, config, tmp_path) -> None:
    for index in range(5):
        _candidate(conn, f"M{index}", caption=f"퇴근 후 저녁 {index} #퇴근", username=f"user{index}")
    cfg = _config(config, tmp_path, max_candidates_per_run=2)
    result, _ = _run(conn, cfg)

    assert result.processed == 2                    # 나머지는 다음 실행으로 이월


def test_daily_claude_limit_falls_back(conn, config, tmp_path) -> None:
    bump_stat(conn, today_str(9), DAILY_METRIC, 20)
    conn.commit()
    _candidate(conn)
    result, runner = _run(conn, _config(config, tmp_path))

    assert runner.calls == []
    assert result.analyzed == 1 and result.fallbacks >= 1   # heuristic으로 진행


def test_claude_unavailable_falls_back(conn, config, tmp_path) -> None:
    _candidate(conn)
    result, runner = _run(conn, _config(config, tmp_path), runner=FakeRunner(available=False))

    assert runner.calls == [] and result.analyzed == 1
    assert result.status == STATUS_OK


def test_timeout_falls_back_without_retry(conn, config, tmp_path) -> None:
    timeout = RunnerResult(ok=False, failure=FailureReason.TIMEOUT, detail="120s")
    _candidate(conn)
    result, runner = _run(conn, _config(config, tmp_path), runner=FakeRunner(result=timeout))

    assert len(runner.calls) == 1 and result.analyzed == 1


# --- 11. 오류 격리 -----------------------------------------------------------
def test_one_candidate_error_does_not_stop_run(conn, config, tmp_path) -> None:
    ok_pk = _candidate(conn, "OK1")
    bad_pk = _candidate(conn, "BAD1", username="creator_b")

    class PartlyFailing(CandidateProcessor):
        def process(self, media_pk: int):
            if media_pk == bad_pk:
                raise RuntimeError("의도된 실패")
            return super().process(media_pk)

    def factory(cfg, connection):
        from targeting_agent.analysis.profile_analyzer import load_profile

        profile = load_profile(cfg.profile_path, connection)
        return PartlyFailing(
            cfg, connection, profile=profile,
            intelligence=ClaudeIntelligence(cfg, profile, runner=FakeRunner()), seed=1,
        )

    result = run_scheduled(_config(config, tmp_path), conn, processor_factory=factory)

    # 한 건이 예외로 죽어도 실행은 계속되고, 나머지는 정상 처리된다
    assert result.status == STATUS_OK and result.exit_code == 0
    assert result.processed == 2 and result.errors == 1
    assert any(str(bad_pk) in detail for detail in result.details)
    assert conn.execute(
        "SELECT status FROM candidate_media WHERE media_pk = ?", (ok_pk,)
    ).fetchone()[0] == MediaStatus.SCORED.value
    assert Path(result.summary_path).exists()          # 요약은 그대로 생성된다


# --- 12~18. Summary 내용 ------------------------------------------------------
def test_summary_contains_sections(conn, config, tmp_path) -> None:
    media_pk = _candidate(conn)
    enqueue_action(
        conn, run_id="r", media_pk=media_pk, creator_id=1, action_type=ActionType.LIKE,
        target_score=90.0, priority=90, executor="manual", dry_run=True,
    )
    from targeting_agent.learning.feedback import record_feedback

    record_feedback(conn, FeedbackType.GOOD_TARGET, media_pk=media_pk, creator_id=1)
    conn.commit()

    result, _ = _run(conn, _config(config, tmp_path))
    html = Path(result.summary_path).read_text(encoding="utf-8")

    for section in ("Candidate", "AI (Claude Code)", "Score", "Action Queue", "Feedback", "Inbox"):
        assert section in html
    assert "GOOD_TARGET" in html
    assert "승인 대기" in html and "실행 대기(승인됨)" in html
    assert "Scheduled Run은 Action을 실행하지 않는다" in html


def test_summary_escapes_user_input(conn, config, tmp_path) -> None:
    _candidate(conn, "XSS", caption="<script>alert('x')</script> #퇴근", username="<b>bad</b>")
    from targeting_agent.learning.feedback import record_feedback

    record_feedback(
        conn, FeedbackType.NOT_MY_STYLE, media_pk=1, creator_id=1, note="<img src=x onerror=1>"
    )
    conn.commit()

    result, _ = _run(conn, _config(config, tmp_path))
    html = Path(result.summary_path).read_text(encoding="utf-8")

    assert "<script>" not in html and "onerror" not in html


def test_summary_counts_ai_usage(conn, config, tmp_path) -> None:
    _candidate(conn)
    result, _ = _run(conn, _config(config, tmp_path))
    data = build_summary_data(conn, _config(config, tmp_path), today_str(9))

    assert data.ai["claude_calls"] >= 1
    assert data.ai["claude_analyzed"] >= 1
    assert "Claude 호출" in render_summary_html(data) or result.claude_calls >= 1


def test_summary_inbox_stats(conn, config, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "b.csv").write_text(
        "url\nhttps://www.instagram.com/reel/AAA/\nnot-a-url\n", encoding="utf-8"
    )
    result, _ = _run(conn, _config(config, tmp_path))
    html = Path(result.summary_path).read_text(encoding="utf-8")

    assert "Invalid" in html and result.ingest.invalid == 1


# --- 19~20. 리포트 파일 관리 ---------------------------------------------------
def test_cleanup_only_touches_daily_summary_files(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path, summary={"enabled": True, "output_dir": str(tmp_path / "reports"), "keep_days": 1})
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    old = reports / "daily_summary_20200101.html"
    keep = reports / "keep_me.txt"
    old.write_text("old", encoding="utf-8")
    keep.write_text("other", encoding="utf-8")
    ancient = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    import os

    os.utime(old, (ancient, ancient))
    os.utime(keep, (ancient, ancient))

    removed = cleanup_old_reports(cfg)
    assert removed == 1 and not old.exists()
    assert keep.exists()                              # 다른 파일은 건드리지 않는다


# --- 21~24. Lock --------------------------------------------------------------
def test_lock_prevents_concurrent_run(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)
    lock = SchedulerLock(path=Path(cfg.get("scheduler.lock.path")), stale_minutes=60)
    assert lock.acquire() is True

    result, runner = _run(conn, cfg)
    assert result.status == STATUS_LOCKED and result.exit_code == 0
    assert runner.calls == []
    lock.release()


def test_stale_lock_is_taken_over(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)
    path = Path(cfg.get("scheduler.lock.path"))
    path.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    path.write_text(f'{{"pid": 1, "created_at": "{old}"}}', encoding="utf-8")

    result, _ = _run(conn, cfg)
    assert result.status == STATUS_OK


def test_lock_released_after_run(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)
    _run(conn, cfg)
    assert not Path(cfg.get("scheduler.lock.path")).exists()


def test_lock_released_after_failure(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)

    def failing(config_, conn_):
        raise RuntimeError("처리기 생성 실패")

    _candidate(conn)
    result = run_scheduled(cfg, conn, processor_factory=failing)

    assert result.status == "FAILED" and result.exit_code == 1
    assert not Path(cfg.get("scheduler.lock.path")).exists()   # 비정상 종료에도 정리


def test_corrupt_lock_treated_as_stale(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)
    path = Path(cfg.get("scheduler.lock.path"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("깨진 내용", encoding="utf-8")

    result, _ = _run(conn, cfg)
    assert result.status == STATUS_OK


# --- 25~27. 실행 환경 / 기록 ----------------------------------------------------
def test_run_recorded_in_scheduled_runs(conn, config, tmp_path) -> None:
    _candidate(conn)
    result, _ = _run(conn, _config(config, tmp_path))
    row = conn.execute("SELECT * FROM scheduled_runs ORDER BY run_id DESC LIMIT 1").fetchone()

    assert row["status"] == STATUS_OK
    assert row["candidates_processed"] == 1
    assert row["summary_file"] == result.summary_path
    assert row["finished_at"]


def test_scheduler_does_not_execute_actions(conn, config, tmp_path) -> None:
    """승인된 Action이 있어도 Scheduled Run은 실행하지 않는다."""
    media_pk = _candidate(conn)
    action_id = enqueue_action(
        conn, run_id="r", media_pk=media_pk, creator_id=1, action_type=ActionType.LIKE,
        target_score=90.0, priority=90, executor="manual", dry_run=True,
    )
    conn.execute(
        "UPDATE action_queue SET approved_at = ? WHERE action_id = ?", (utc_now(), action_id)
    )
    conn.commit()

    _run(conn, _config(config, tmp_path))

    assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0
    row = conn.execute("SELECT status, approved_at FROM action_queue").fetchone()
    assert row["status"] == "PENDING" and row["approved_at"]   # 승인 상태도 그대로


def test_disabled_scheduler_is_noop(conn, config, tmp_path) -> None:
    _candidate(conn)
    cfg = _config(config, tmp_path, enabled=False)
    result, runner = _run(conn, cfg)

    assert result.exit_code == 0 and result.processed == 0
    assert runner.calls == [] and result.summary_path is None
