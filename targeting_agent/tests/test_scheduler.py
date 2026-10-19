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


def test_락_소유자가_없으면_TTL과_무관하게_회수한다(conn, config, tmp_path) -> None:
    """락을 쥔 프로세스가 없으면 기록이 오래됐든 말든 인수한다(P1-1: 판정은 커널이 한다)."""
    cfg = _config(config, tmp_path)
    path = Path(cfg.get("scheduler.lock.path"))
    path.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    path.write_text(f'{{"pid": 1, "created_at": "{old}"}}', encoding="utf-8")

    result, _ = _run(conn, cfg)
    assert result.status == STATUS_OK


def test_락_소유자가_없으면_신선한_기록이어도_회수한다(conn, config, tmp_path) -> None:
    """P1-1의 표현: 방금 시각의 기록이어도 소유자가 없으면 즉시 실행한다(예전에는 60분 차단)."""
    cfg = _config(config, tmp_path)
    path = Path(cfg.get("scheduler.lock.path"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(f'{{"pid": 1, "created_at": "{fresh}"}}', encoding="utf-8")

    result, _ = _run(conn, cfg)
    assert result.status == STATUS_OK


def _assert_released_not_deleted(path: Path) -> None:
    """해제 후에도 파일은 남고(unlink 금지: inode 동일성) released_at 이 있으며 다시 잡을 수 있다."""
    assert path.exists()
    assert "released_at" in json.loads(path.read_text(encoding="utf-8"))
    again = SchedulerLock(path=path, stale_minutes=60)
    assert again.acquire() is True
    again.release()


def test_lock_released_after_run(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)
    _run(conn, cfg)
    _assert_released_not_deleted(Path(cfg.get("scheduler.lock.path")))


def test_lock_released_after_failure(conn, config, tmp_path) -> None:
    cfg = _config(config, tmp_path)

    def failing(config_, conn_):
        raise RuntimeError("처리기 생성 실패")

    _candidate(conn)
    result = run_scheduled(cfg, conn, processor_factory=failing)

    assert result.status == "FAILED" and result.exit_code == 1
    _assert_released_not_deleted(Path(cfg.get("scheduler.lock.path")))   # 비정상 종료에도 해제


def test_깨진_lock_파일이라도_소유자가_없으면_차단하지_않는다(conn, config, tmp_path) -> None:
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


def test_lock_로그에_어느_실행인지_나온다(tmp_path, caplog):
    """같은 lock 클래스를 여러 실행이 쓰므로, 문구로 소유자가 분간돼야 한다.

    실기에서 Discovery가 막혔는데 '다른 Scheduled Run이 진행 중'이라고 찍혀 혼동됐다.
    P1-1: 손으로 쓴 파일이 아니라 실제로 락을 쥔 프로세스(자식)를 소유자로 쓴다.
    """
    import logging

    path = tmp_path / ".browser_discovery.lock"
    lock = SchedulerLock(path=path, stale_minutes=30, label="Browser Discovery")

    with _holder(path, label="Browser Discovery", stale_minutes=30) as child:
        with caplog.at_level(logging.INFO):
            acquired = lock.acquire()

    assert acquired is False
    message = caplog.text
    assert "Browser Discovery" in message
    assert "Scheduled Run" not in message
    assert str(child.pid) in message                 # 락을 쥔 프로세스의 pid
    assert str(path) in message                      # 어느 lock 파일인지
    assert "지우지 마십시오" in message               # 복구 안내가 역전됐다(삭제는 위험하다)


def test_모든_runtime이_lock_소유자_이름을_넘긴다():
    """lock을 쓰는 Runtime은 자기 이름을 넘겨야 한다.

    Scheduler만 기본값("Scheduled Run")을 쓴다. 다른 Runtime이 기본값에 의존하면
    차단 로그가 엉뚱한 주체를 가리켜, 운영자가 stale lock 원인을 잘못 짚는다.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    기본값_허용 = {root / "scheduler" / "runner.py"}

    사용처 = []
    for source in sorted(root.rglob("*.py")):
        if "tests" in source.parts:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            이름 = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if 이름 != "SchedulerLock":
                continue
            사용처.append(source)
            if source in 기본값_허용:
                continue
            labels = [kw for kw in node.keywords if kw.arg == "label"]
            assert labels, f"{source.relative_to(root)}:{node.lineno} 가 label을 넘기지 않는다"
            assert isinstance(labels[0].value, ast.Constant) and labels[0].value.value, (
                f"{source.relative_to(root)}:{node.lineno} 의 label이 비어 있다"
            )

    assert len(사용처) >= 5  # 사용처를 지나치게 좁게 찾고 있지 않은지 자체 점검


# --- P1-1. OS 파일락 -----------------------------------------------------------
import contextlib
import json
import logging
import os
import subprocess
import sys
import threading

from targeting_agent.scheduler import lock as lockmod

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HOLDER_SCRIPT = """
import os, sys, time
from pathlib import Path
from targeting_agent.scheduler.lock import SchedulerLock
lock = SchedulerLock(path=Path(sys.argv[1]), stale_minutes=int(sys.argv[2]), label=sys.argv[3])
ok = lock.acquire()
print("READY %d" % os.getpid() if ok else "DENIED", flush=True)
time.sleep(120)
"""


@contextlib.contextmanager
def _holder(path: Path, label: str = "Holder", stale_minutes: int = 60):
    """별도 프로세스가 실제로 락을 잡고 대기한다. 빠져나올 때 반드시 종료한다."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(path), str(stale_minutes), label],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # 자식이 출력 전에 멈춰도 테스트가 무한 대기하지 않게 한다(finally에서 kill하면 EOF로 풀린다).
        box: list[str] = []
        reader = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=30)
        assert box, "자식이 30초 안에 락 획득 결과를 출력하지 않았다"
        line = box[0].strip()
        assert line == f"READY {proc.pid}", f"자식이 락을 잡지 못했다: {line!r}"
        yield proc
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_T1_다른_프로세스가_락을_쥐면_차단되고_죽으면_TTL_없이_즉시_인수한다(tmp_path, caplog):
    """P1-1 수용 기준. 실제 두 프로세스, 실제 커널 락."""
    path = tmp_path / ".scheduler.lock"
    lock = SchedulerLock(path=path, stale_minutes=60, label="Holder")

    with _holder(path) as child:
        assert child.pid != os.getpid()                      # 정말 다른 프로세스다
        created_at = _payload(path)["created_at"]
        with caplog.at_level(logging.INFO):
            assert lock.acquire() is False
        text = caplog.text
        assert "Holder" in text and str(child.pid) in text and str(path) in text
        assert created_at in text and "60" in text           # 생성 시각·TTL
        assert "프로세스를 종료하면 즉시 해제됩니다" in text
        assert "지우지 마십시오" in text
        assert _payload(path)["pid"] == child.pid            # 차단이 소유자 기록을 건드리지 않았다

        child.kill()                                         # SIGKILL: 정리 코드가 돌지 않는다
        child.wait()
        caplog.clear()
        assert lock.acquire() is True                        # TTL(60분) 경과 없이 즉시
    assert _payload(path)["pid"] == os.getpid()
    lock.release()


def test_T2_같은_프로세스의_두_번째_인스턴스도_거부된다(tmp_path):
    path = tmp_path / ".scheduler.lock"
    first = SchedulerLock(path=path)
    second = SchedulerLock(path=path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_T3_release는_파일을_지우지_않고_released_at을_남긴다(tmp_path, caplog):
    path = tmp_path / ".scheduler.lock"
    lock = SchedulerLock(path=path)
    assert lock.acquire() is True
    lock.release()

    assert path.exists()                                     # unlink 금지(inode 동일성)
    assert "released_at" in _payload(path)
    with caplog.at_level(logging.INFO):
        assert SchedulerLock(path=path).acquire() is True
    assert "소유자 없음" not in caplog.text                    # 정상 해제는 이상 신호가 아니다


def test_T3b_release는_멱등하고_acquire_실패_뒤에도_안전하다(tmp_path):
    path = tmp_path / ".scheduler.lock"
    holder = SchedulerLock(path=path)
    assert holder.acquire() is True
    loser = SchedulerLock(path=path)
    assert loser.acquire() is False
    loser.release()                                          # 실패한 인스턴스의 release는 no-op
    assert _payload(path)["pid"] == os.getpid() and "released_at" not in _payload(path)

    holder.release()
    first = _payload(path)
    holder.release()                                         # 2회 호출도 no-op
    assert _payload(path) == first


def test_T3c_비활성이면_파일을_열지_않는다(tmp_path):
    path = tmp_path / "sub" / ".scheduler.lock"
    lock = SchedulerLock(path=path, enabled=False)
    assert lock.acquire() is True
    lock.release()
    assert not path.exists() and lock._fd is None


def test_T4_SIGKILL된_소유자의_파일에는_released_at이_없고_인수_시_소유자_없음이_로그에_남는다(tmp_path, caplog):
    path = tmp_path / ".scheduler.lock"
    with _holder(path) as child:
        child.kill()
        child.wait()
    assert "released_at" not in _payload(path)
    with caplog.at_level(logging.INFO):
        lock = SchedulerLock(path=path)
        assert lock.acquire() is True
    assert "소유자 없음" in caplog.text and str(child.pid) in caplog.text
    lock.release()


def test_T5_락_없는_신선한_기록은_TTL과_무관하게_인수하고_신호를_남긴다(tmp_path, caplog):
    path = tmp_path / ".scheduler.lock"
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(f'{{"pid": 1, "created_at": "{fresh}"}}', encoding="utf-8")
    with caplog.at_level(logging.INFO):
        lock = SchedulerLock(path=path, stale_minutes=60)
        assert lock.acquire() is True
    assert "초 전인데 커널 락이 비어 있다" in caplog.text        # 락 미지원 FS 탐지기
    assert _payload(path)["pid"] == os.getpid()
    lock.release()


def test_T6_서로_다른_두_lock_파일은_한_프로세스가_동시에_쥘_수_있다(tmp_path):
    outer = SchedulerLock(path=tmp_path / ".autopilot.lock", label="Autopilot")
    inner = SchedulerLock(path=tmp_path / ".browser_discovery.lock", label="Browser Discovery")
    assert outer.acquire() is True
    assert inner.acquire() is True                           # autopilot이 discovery를 내부 호출
    inner.release()
    outer.release()


def test_T7_fd는_상속되지_않아_자식_프로세스가_락을_물지_않는다(tmp_path):
    path = tmp_path / ".scheduler.lock"
    lock = SchedulerLock(path=path)
    assert lock.acquire() is True
    assert os.get_inheritable(lock._fd) is False

    # POSIX에서는 close_fds=False 로 띄워 '상속 가능 여부'만이 결과를 가르게 한다.
    # 주의: Windows에서는 redirect 없는 close_fds=False 가 기본과 같아, 이 테스트는
    # get_inheritable 단정만 검증한다(자식 프로세스 상속 검증은 POSIX에서만 유효하다).
    extra = {"close_fds": False} if os.name == "posix" else {}
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], **extra
    )
    try:
        lock.release()
        again = SchedulerLock(path=path)
        assert again.acquire() is True                       # 고아 자식이 락을 물고 있지 않다
        again.release()
    finally:
        orphan.kill()
        orphan.wait()


def test_T8_락을_쥔_중에도_다른_프로세스가_offset_1부터_payload를_읽는다(tmp_path):
    path = tmp_path / ".scheduler.lock"
    with _holder(path) as child:
        with open(path, "rb") as fh:
            fh.seek(1)
            assert json.loads(fh.read().decode("utf-8"))["pid"] == child.pid
        assert SchedulerLock(path=path)._read()["pid"] == child.pid


def test_T12_payload는_전체_읽기와_offset_1_읽기_모두_유효한_JSON이다(tmp_path):
    path = tmp_path / ".scheduler.lock"
    lock = SchedulerLock(path=path, label="한글 label")
    assert lock.acquire() is True
    raw = path.read_bytes()
    assert raw[:1] == b" "                                   # Windows 락 바이트(자리바이트)
    full = json.loads(raw.decode("utf-8"))
    tail = json.loads(raw[1:].decode("utf-8"))
    assert full == tail and full["pid"] == os.getpid() and full["label"] == "한글 label"
    lock.release()


def test_T12b_락_바이트를_읽을_수_없어도_offset_1로_읽는다(tmp_path, monkeypatch):
    """Windows 바이트영역 락은 mandatory라 offset 0을 포함한 읽기가 실패한다."""
    path = tmp_path / ".scheduler.lock"
    holder = SchedulerLock(path=path)
    assert holder.acquire() is True
    real = lockmod._read_bytes

    def mandatory(p, offset):
        if offset == 0:
            raise PermissionError("lock violation")
        return real(p, offset)

    monkeypatch.setattr(lockmod, "_read_bytes", mandatory)
    assert SchedulerLock(path=path)._read()["pid"] == os.getpid()
    holder.release()


def test_읽기는_항상_offset_1을_먼저_시도하고_예전_형식만_전체를_다시_읽는다(tmp_path, monkeypatch):
    new_fmt = tmp_path / "new.lock"
    new_fmt.write_text(' {"pid": 11, "created_at": "2026-01-01T00:00:00+00:00"}', encoding="utf-8")
    old_fmt = tmp_path / "old.lock"
    old_fmt.write_text('{"pid": 22, "created_at": "2026-01-01T00:00:00+00:00"}', encoding="utf-8")
    real = lockmod._read_bytes
    offsets: list[int] = []

    def spy(p, offset):
        offsets.append(offset)
        return real(p, offset)

    monkeypatch.setattr(lockmod, "_read_bytes", spy)
    assert SchedulerLock(path=new_fmt)._read()["pid"] == 11
    assert offsets == [1]                                    # 새 형식: offset 1 한 번
    offsets.clear()
    assert SchedulerLock(path=old_fmt)._read()["pid"] == 22
    assert offsets == [1, 0]                                 # 예전 형식: offset 1 실패 후 전체


def test_M1_offset_1은_읽히는데_전체_읽기가_OSError면_깨진_기록이_아니라_UNREADABLE이다(tmp_path, monkeypatch):
    """그 조합의 현실적 원인은 '바이트 0 mandatory lock = 소유자 생존'뿐이다. stale로 보면 회수한다."""
    path = tmp_path / ".scheduler.lock"
    path.write_bytes(b" ")                                  # 소유자가 자리바이트만 쓴 상태
    real = lockmod._read_bytes

    def mandatory(p, offset):
        if offset == 0:
            raise PermissionError("lock violation")
        return real(p, offset)

    monkeypatch.setattr(lockmod, "_read_bytes", mandatory)
    assert SchedulerLock(path=path)._read() is lockmod.UNREADABLE
    # 폴백(UNKNOWN)이 이 상태를 만나도 회수하지 않는다.
    monkeypatch.setattr(lockmod, "_LOCKER", None)
    assert SchedulerLock(path=path, stale_minutes=60).acquire() is False
    monkeypatch.undo()
    assert path.read_bytes() == b" "


def test_예전_형식_lock_파일도_pid를_읽는다(tmp_path):
    """자리바이트 없는 예전 `{...}` 파일은 offset 1에서 첫 글자가 잘리므로 전체를 다시 읽는다."""
    path = tmp_path / ".scheduler.lock"
    path.write_text('{"pid": 77, "created_at": "2026-01-01T00:00:00+00:00"}', encoding="utf-8")
    assert SchedulerLock(path=path)._read()["pid"] == 77


# --- 주입 테스트: Windows 분기 / 폴백 / 읽기 실패 / I/O 실패 -------------------------
class FakeMsvcrt:
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, fail_errno: int | None = None) -> None:
        self.fail_errno = fail_errno
        self.calls: list[tuple[int, int, int]] = []   # (mode, nbytes, 호출 시점의 파일 위치)

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == self.LK_NBLCK and self.fail_errno is not None:
            raise OSError(self.fail_errno, "fake msvcrt")


def _use_windows_locker(monkeypatch, fake: FakeMsvcrt) -> None:
    monkeypatch.setattr(
        lockmod, "_LOCKER", (lockmod._win_lock, lockmod._win_unlock, fake)
    )


def _write_record(path: Path, age: timedelta, **extra) -> str:
    created = (datetime.now(timezone.utc) - age).isoformat(timespec="seconds")
    text = json.dumps({"pid": 4242, "created_at": created, **extra})
    path.write_text(" " + text, encoding="utf-8")
    return text


def test_T9_Windows_분기_정상_획득은_바이트0을_1바이트_NBLCK으로_잠그고_release에서_푼다(tmp_path, monkeypatch):
    fake = FakeMsvcrt()
    _use_windows_locker(monkeypatch, fake)
    lock = SchedulerLock(path=tmp_path / ".scheduler.lock")
    assert lock.acquire() is True
    # 자리바이트를 쓴 직후라 위치가 1이다 → lseek(0) 이 선행돼야 바이트 0이 잠긴다.
    assert fake.calls == [(FakeMsvcrt.LK_NBLCK, 1, 0)]
    lock.release()
    assert fake.calls[-1] == (FakeMsvcrt.LK_UNLCK, 1, 0)
    assert lock._fd is None and "released_at" in _payload(tmp_path / ".scheduler.lock")


def test_release는_acquire_시점의_locker로_unlock한다(tmp_path, monkeypatch):
    """release에서 모듈 전역 locker를 다시 읽으면 acquire 때와 어긋난다(결합 회귀 방지)."""
    first, other = FakeMsvcrt(), FakeMsvcrt()
    _use_windows_locker(monkeypatch, first)
    lock = SchedulerLock(path=tmp_path / ".scheduler.lock")
    assert lock.acquire() is True
    _use_windows_locker(monkeypatch, other)          # acquire 이후 전역이 바뀐다
    lock.release()
    assert first.calls[-1] == (FakeMsvcrt.LK_UNLCK, 1, 0)
    assert other.calls == []
    monkeypatch.setattr(lockmod, "_LOCKER", None)    # 전역이 사라져도 release는 깨지지 않는다
    again = SchedulerLock(path=tmp_path / "b.lock")
    assert again.acquire() is True
    again.release()


def test_unlock이_실패해도_close는_수행되고_폴백_claim은_unlock하지_않는다(tmp_path, monkeypatch):
    import errno

    class BrokenUnlock(FakeMsvcrt):
        def locking(self, fd, mode, nbytes):
            if mode == self.LK_UNLCK:
                raise OSError(errno.EBADF, "unlock failed")
            super().locking(fd, mode, nbytes)

    _use_windows_locker(monkeypatch, BrokenUnlock())
    lock = SchedulerLock(path=tmp_path / ".scheduler.lock")
    assert lock.acquire() is True
    fd = lock._fd
    lock.release()
    with pytest.raises(OSError):
        os.fstat(fd)                                 # fd가 닫혔다

    fallback = FakeMsvcrt(fail_errno=errno.EINVAL)
    _use_windows_locker(monkeypatch, fallback)
    claimed = SchedulerLock(path=tmp_path / "fb.lock")
    assert claimed.acquire() is True and claimed._unlock is None
    claimed.release()
    assert all(call[0] != FakeMsvcrt.LK_UNLCK for call in fallback.calls)   # 쥔 락이 없다


def test_T9_Windows_분기_EACCES는_차단이고_TTL이_지나도_회수하지_않는다(tmp_path, monkeypatch, caplog):
    import errno

    path = tmp_path / ".scheduler.lock"
    original = _write_record(path, timedelta(hours=5))       # TTL(60분)을 한참 넘긴 기록
    _use_windows_locker(monkeypatch, FakeMsvcrt(fail_errno=errno.EACCES))
    with caplog.at_level(logging.INFO):
        assert SchedulerLock(path=path, stale_minutes=60).acquire() is False
    assert path.read_text(encoding="utf-8") == " " + original   # 회수·덮어쓰기 없음
    assert "회수하지 않습니다" in caplog.text and "4242" in caplog.text


def test_T9_Windows_분기_EINVAL은_UNKNOWN이라_TTL_폴백한다(tmp_path, monkeypatch, caplog):
    import errno

    path = tmp_path / ".scheduler.lock"
    _write_record(path, timedelta(hours=5))
    _use_windows_locker(monkeypatch, FakeMsvcrt(fail_errno=errno.EINVAL))
    with caplog.at_level(logging.INFO):
        lock = SchedulerLock(path=path, stale_minutes=60)
        assert lock.acquire() is True                         # 오래된 기록 → 오늘처럼 회수
    assert "mode=TTL_FALLBACK" in caplog.text and "lock 기반 판정 비활성" in caplog.text
    assert lock._mode == lockmod.MODE_TTL_FALLBACK
    lock.release()
    assert "released_at" in _payload(path)


def test_T9_방금_만든_파일에서_충돌이_보고되면_빈_기록이어도_claim하지_않고_차단한다(tmp_path, monkeypatch, caplog):
    """방금 만든 파일에서 충돌 = 락 영역 오판 신호이거나 다른 실행이 초기화 중이다.
    어느 쪽이든 커널 락 없이 claim하면 안 된다(이후 실행이 2중 소유한다)."""
    import errno

    _use_windows_locker(monkeypatch, FakeMsvcrt(fail_errno=errno.EACCES))
    path = tmp_path / ".scheduler.lock"
    assert not path.exists()
    with caplog.at_level(logging.INFO):
        lock = SchedulerLock(path=path)
        assert lock.acquire() is False
    assert path.read_bytes() == b" "                          # 자리바이트뿐, 덮어쓰지 않았다
    assert "락 영역 오판" in caplog.text and "이번 실행은 건너뜁니다" in caplog.text
    assert lock._fd is None and lock._acquired is False


def _contended_after_writing(record: dict):
    """락 시도 직전에 '다른 실행'이 payload를 써 두고 충돌을 보고하는 locker(경쟁 모사)."""
    import errno

    def lock(fd, mod):
        data = b" " + json.dumps(record).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)
        return lockmod.CONTENDED, f"errno={errno.EACCES}"

    return (lock, lambda fd, mod: None, None)


def test_T9_방금_만든_파일에서_충돌이_보고되면_기록이_있어도_커널_락_없이_claim하지_않는다(tmp_path, monkeypatch):
    """released_at 기록을 본 쪽이 커널 락 없이 claim하면 뒤 실행(KERNEL)과 동시에 돈다(L1)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for name, record in {
        "fresh": {"pid": 4242, "created_at": now},
        "released": {"pid": 4242, "created_at": now, "released_at": now},
    }.items():
        monkeypatch.setattr(lockmod, "_LOCKER", _contended_after_writing(record))
        path = tmp_path / f"{name}.lock"
        assert SchedulerLock(path=path, stale_minutes=60).acquire() is False, name
        assert _payload(path) == record                      # 기록을 덮어쓰지 않았다


def test_H1_TTL_폴백으로_claim한_실행이_있으면_뒤따르는_KERNEL_실행은_차단된다(tmp_path, monkeypatch, caplog):
    """폴백 claim자는 커널 락을 쥐지 않는다. 기록에 mode를 남겨 다음 실행이 TTL로 보호한다."""
    import errno

    path = tmp_path / ".scheduler.lock"
    real_locker = lockmod._LOCKER
    _use_windows_locker(monkeypatch, FakeMsvcrt(fail_errno=errno.EINVAL))   # 일시적 락 오류
    a = SchedulerLock(path=path, stale_minutes=60, label="Holder")
    assert a.acquire() is True and a._mode == lockmod.MODE_TTL_FALLBACK
    assert _payload(path)["mode"] == lockmod.MODE_TTL_FALLBACK

    monkeypatch.setattr(lockmod, "_LOCKER", real_locker)                    # 락이 정상으로 돌아옴
    with caplog.at_level(logging.INFO):
        b = SchedulerLock(path=path, stale_minutes=60, label="Holder")
        assert b.acquire() is False                          # 구버전처럼 신선한 기록 앞에서 차단
    assert _payload(path)["pid"] == os.getpid() and "released_at" not in _payload(path)
    assert "TTL" in caplog.text and "지우지 마십시오" in caplog.text

    a.release()                                              # 정상 해제되면 바로 인수된다
    c = SchedulerLock(path=path, stale_minutes=60)
    assert c.acquire() is True and c._mode == lockmod.MODE_KERNEL
    assert _payload(path)["mode"] == lockmod.MODE_KERNEL
    c.release()


def test_H1_폴백_기록이_TTL을_넘기면_KERNEL_실행이_인수한다(tmp_path):
    path = tmp_path / ".scheduler.lock"
    _write_record(path, timedelta(hours=3), mode=lockmod.MODE_TTL_FALLBACK)
    lock = SchedulerLock(path=path, stale_minutes=60)
    assert lock.acquire() is True and lock._mode == lockmod.MODE_KERNEL
    lock.release()


def test_H1_KERNEL_소유자의_기록은_신선해도_TTL로_보지_않고_즉시_인수한다(tmp_path):
    path = tmp_path / ".scheduler.lock"
    _write_record(path, timedelta(seconds=1), mode=lockmod.MODE_KERNEL)   # 죽은 KERNEL 소유자
    lock = SchedulerLock(path=path, stale_minutes=60)
    assert lock.acquire() is True
    lock.release()


def test_H2_기록이_빈_채_충돌이_반복되면_매번_WARNING과_실행_가능한_안내가_나온다(tmp_path, monkeypatch, caplog):
    """락 영역을 상시 오판하는 FS 모사. 1회차(파일 생성)뿐 아니라 2회차 이후에도 알려야 한다."""
    import errno

    _use_windows_locker(monkeypatch, FakeMsvcrt(fail_errno=errno.EACCES))
    path = tmp_path / ".scheduler.lock"
    for attempt in range(3):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert SchedulerLock(path=path, label="Probe").acquire() is False
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        text = " ".join(warnings)
        assert "소유자 기록이 비어 있습니다" in text, attempt
        assert "반복되면" in text and "로컬 디스크" in text and str(path) in text, attempt
        assert "지우지 마십시오" in text
        assert "종료하" not in text, attempt                 # pid가 없으니 종료하라는 안내는 틀렸다
    assert path.read_bytes() == b" "


def test_일반_폴백은_빈_기록도_회수한다(tmp_path, monkeypatch):
    """안전망을 거치지 않은 UNKNOWN(EINVAL 등)에서는 새 파일의 빈 기록을 stale로 본다(탈출구)."""
    import errno

    _use_windows_locker(monkeypatch, FakeMsvcrt(fail_errno=errno.EINVAL))
    lock = SchedulerLock(path=tmp_path / ".scheduler.lock")
    assert lock.acquire() is True
    lock.release()


def test_모드_로그_한_줄이_KERNEL을_알린다(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        lock = SchedulerLock(path=tmp_path / ".scheduler.lock")
        assert lock.acquire() is True
    assert "mode=KERNEL" in caplog.text
    assert lock._mode == lockmod.MODE_KERNEL
    lock.release()


def test_T10_락_API가_없으면_오늘의_TTL_의미가_그대로다(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(lockmod, "_LOCKER", None)
    old = tmp_path / "old.lock"
    _write_record(old, timedelta(hours=3))
    fresh = tmp_path / "fresh.lock"
    original = _write_record(fresh, timedelta(minutes=1))
    broken = tmp_path / "broken.lock"
    broken.write_text("깨진 내용", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        a = SchedulerLock(path=old, stale_minutes=60)
        assert a.acquire() is True                            # 오래된 기록은 회수
        c = SchedulerLock(path=broken, stale_minutes=60)
        assert c.acquire() is True                            # 깨진 기록(JSONDecodeError)도 stale
        assert SchedulerLock(path=fresh, stale_minutes=60).acquire() is False   # 신선한 기록은 차단
    assert fresh.read_text(encoding="utf-8") == " " + original
    assert "mode=TTL_FALLBACK" in caplog.text
    a.release()
    c.release()
    # 폴백에서도 정상 해제된 기록은 곧바로 인수된다
    again = SchedulerLock(path=old, stale_minutes=60)
    assert again.acquire() is True
    again.release()


def test_T11_락을_쥔_채_payload를_읽을_수_없어도_차단하고_회수하지_않는다(tmp_path, monkeypatch, caplog):
    path = tmp_path / ".scheduler.lock"
    holder = SchedulerLock(path=path)
    assert holder.acquire() is True
    before = path.read_bytes()

    def broken(p, offset):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(lockmod, "_read_bytes", broken)
    with caplog.at_level(logging.INFO):
        assert SchedulerLock(path=path, label="Probe").acquire() is False
    assert path.read_bytes() == before
    assert "소유자 기록을 읽을 수 없습니다(락 보유 중)" in caplog.text
    assert "Probe" in caplog.text and str(path) in caplog.text and "지우지 마십시오" in caplog.text
    holder.release()


def test_락_경로는_TTL이_지나도_회수하지_않고_로그_레벨만_올린다(tmp_path, caplog):
    path = tmp_path / ".scheduler.lock"
    holder = SchedulerLock(path=path)
    assert holder.acquire() is True
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    with caplog.at_level(logging.INFO):
        assert SchedulerLock(path=path, stale_minutes=60).acquire(later) is False
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("비정상적으로 긴 실행" in r.getMessage() and "회수하지 않습니다" in r.getMessage() for r in warned)
    assert _payload(path)["pid"] == os.getpid()
    holder.release()


def test_T13_lock_파일을_열지_못하면_False가_아니라_LockIOError다(tmp_path, monkeypatch, caplog, conn, config):
    cfg = _config(config, tmp_path)
    path = Path(cfg.get("scheduler.lock.path"))
    real_open = os.open

    def deny(p, *args, **kwargs):
        if str(p) == str(path):
            raise PermissionError(13, "denied")
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr(lockmod.os, "open", deny)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(lockmod.LockIOError):
            SchedulerLock(path=path).acquire()
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        # 호출부도 삼키지 않는다 — LOCKED/exit 0 으로 둔갑하면 Task Scheduler가 성공으로 본다.
        with pytest.raises(lockmod.LockIOError):
            _run(conn, cfg)


def test_T14_폴백_경로에서_기록을_읽을_수_없으면_회수하지_않고_차단한다(tmp_path, monkeypatch, caplog):
    path = tmp_path / ".scheduler.lock"
    original = _write_record(path, timedelta(hours=5))        # 읽혔다면 stale로 회수됐을 기록
    monkeypatch.setattr(lockmod, "_LOCKER", None)

    def broken(p, offset):
        raise PermissionError("OneDrive placeholder")

    monkeypatch.setattr(lockmod, "_read_bytes", broken)
    with caplog.at_level(logging.INFO):
        assert SchedulerLock(path=path, stale_minutes=60).acquire() is False
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == " " + original
    assert "소유자 기록을 읽을 수 없습니다" in caplog.text


def test_5개_Runtime_모두_실제_락을_쥔_상태에서는_각자의_LOCKED로_끝난다(conn, config, tmp_path):
    """교차 회귀: 락 구현이 바뀌어도 Runtime별 LOCKED 상태와 exit 0 계약이 같다."""
    from targeting_agent.autopilot.runner import STATUS_LOCKED as AUTOPILOT_LOCKED, run_autopilot
    from targeting_agent.canary.comment_runner import OUTCOME_LOCKED, run_comment_canary
    from targeting_agent.canary.resume import STEP_NOOP, resume_canary
    from targeting_agent.discovery.browser_runner import (
        STATUS_LOCKED as DISCOVERY_LOCKED,
        run_browser_discovery,
    )

    raw = {
        **config.raw,
        "scheduler": {**config.raw.get("scheduler", {}), "enabled": True,
                      "lock": {"enabled": True, "path": "data/.scheduler.lock", "stale_minutes": 60}},
        "autopilot": {**config.raw.get("autopilot", {}),
                      "lock": {"enabled": True, "path": "data/.autopilot.lock", "stale_minutes": 60}},
        "browser_discovery": {**config.raw.get("browser_discovery", {}), "enabled": True,
                              "lock": {"enabled": True, "path": "data/.browser_discovery.lock",
                                       "stale_minutes": 30}},
        "canary": {**config.raw.get("canary", {}),
                   "comment": {**config.raw.get("canary", {}).get("comment", {}),
                               "lock": {"enabled": True, "path": "data/.canary_comment.lock",
                                        "stale_minutes": 30}}},
    }
    cfg = Config(raw=raw, path=config.path, base_dir=tmp_path)

    names = {
        "scheduler": ("data/.scheduler.lock", lambda: run_scheduled(cfg, conn)),
        "autopilot": ("data/.autopilot.lock", lambda: run_autopilot(cfg, conn)),
        "discovery": ("data/.browser_discovery.lock", lambda: run_browser_discovery(cfg, conn)),
        "comment": ("data/.canary_comment.lock", lambda: run_comment_canary(cfg, conn=conn, live=False)),
        "resume": ("data/.canary_resume.lock", lambda: resume_canary(cfg, conn=conn)),
    }
    holders = []
    try:
        for rel, _ in names.values():
            held = SchedulerLock(path=tmp_path / rel, label="선행 소유자")
            assert held.acquire() is True
            holders.append(held)

        scheduled = names["scheduler"][1]()
        assert scheduled.status == STATUS_LOCKED and scheduled.exit_code == 0
        autopilot = names["autopilot"][1]()
        assert autopilot.status == AUTOPILOT_LOCKED and autopilot.exit_code == 0
        discovery = names["discovery"][1]()
        assert discovery.status == DISCOVERY_LOCKED and discovery.exit_code == 0
        comment = names["comment"][1]()
        assert comment.outcome == OUTCOME_LOCKED and comment.exit_code == 0
        resume = names["resume"][1]()
        assert resume.step == STEP_NOOP and resume.exit_code == 0
    finally:
        for held in holders:
            held.release()


# --- P1-B. 차단 정보를 호출자에게 그대로 전달 ----------------------------------------
def test_blocked_info는_로그에_찍힌_값과_같은_내용이다(tmp_path, caplog):
    path = tmp_path / ".browser_discovery.lock"
    lock = SchedulerLock(path=path, stale_minutes=30, label="Browser Discovery")
    assert lock.blocked_info == {}                    # 차단 전에는 비어 있다

    with _holder(path, label="Browser Discovery", stale_minutes=30) as child:
        with caplog.at_level(logging.INFO):
            assert lock.acquire() is False

    info = lock.blocked_info
    assert info["label"] == "Browser Discovery"
    assert info["path"] == str(path)
    assert info["mode"] == "KERNEL"
    assert info["owner_pid"] == child.pid
    assert info["owner_created_at"] == _payload(path)["created_at"]
    assert info["stale_minutes"] == 30
    # 복구 안내는 lock 이 만든 문장 그대로다 — 로그에도 같은 문장이 있다.
    assert "지우지 마십시오" in info["recovery"]
    assert info["recovery"] in caplog.text
    assert str(child.pid) in caplog.text and str(info["owner_pid"]) in caplog.text
    assert lock.mode == ""                            # 못 잡았으므로 소유 방식은 없다


def test_소유에_성공하면_mode가_노출되고_blocked_info는_비어_있다(tmp_path):
    lock = SchedulerLock(path=tmp_path / ".x.lock", stale_minutes=30)
    assert lock.acquire() is True
    try:
        assert lock.mode == "KERNEL" and lock.blocked_info == {}
    finally:
        lock.release()


def test_소유자_기록이_비어_있을_때도_blocked_info가_로그_문장을_담는다(tmp_path, caplog):
    path = tmp_path / ".x.lock"
    lock = SchedulerLock(path=path, stale_minutes=30, label="Autopilot")
    with caplog.at_level(logging.INFO):
        lock._log_blocked({}, datetime.now(timezone.utc), mode="KERNEL")

    assert lock.blocked_info["owner_pid"] is None
    assert lock.blocked_info["recovery"] in caplog.text
    assert "지우지 마십시오" in lock.blocked_info["recovery"]


def test_lock_인스턴스를_재사용해도_mode와_blocked_info는_이전_값을_남기지_않는다(tmp_path):
    path = tmp_path / ".x.lock"
    lock = SchedulerLock(path=path, stale_minutes=30, label="Autopilot")

    with _holder(path, label="Autopilot", stale_minutes=30):
        assert lock.acquire() is False
        assert lock.blocked_info["owner_pid"] is not None        # 실패한 시도의 값

    assert lock.acquire() is True                                # 성공한 시도는 이전 차단 값을 비운다
    try:
        assert lock.blocked_info == {} and lock.mode == "KERNEL"
    finally:
        lock.release()
    assert lock.mode == ""                                       # 해제하면 소유 방식도 없다

    with _holder(path, label="Autopilot", stale_minutes=30):
        assert lock.acquire() is False
        assert lock.mode == ""                                   # 이전 성공의 mode 가 남지 않는다
