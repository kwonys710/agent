"""Canary Auto-Resume 테스트 (Phase 18E.1).

기다리는 것은 실패가 아니다 — 그 구분을 고정한다.
실제 Instagram에 접속하지 않는다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.canary.resume import (
    STEP_BLOCKED,
    STEP_LIVE,
    STEP_NOOP,
    STEP_REHEARSED,
    STEP_WAITING_BUDGET,
    STEP_WAITING_ELIGIBLE,
    format_resume,
    is_real_instagram_target,
    resume_canary,
)
from targeting_agent.canary.state import (
    COMPLETED,
    READY_FOR_LIVE,
    STOPPED_CHALLENGE,
    STOPPED_LOGIN,
    STOPPED_SAFETY,
    STOPPED_WARNING,
    WAITING_AI_BUDGET,
    WAITING_ELIGIBLE,
    WAITING_NEXT_RUN,
    CanaryState,
    arm,
    load_state,
    save_state,
    state_path,
)
from targeting_agent.core.config import Config
from targeting_agent.discovery import browser_runner
from targeting_agent.discovery.browser_runner import BrowserDiscoveryResult

from .test_live_canary import NOW, FakeInner, _seed  # noqa: F401


# ===========================================================================
# 합성 주소 차단 (§11)
# ===========================================================================
@pytest.mark.parametrize(
    "url, media_id",
    [
        ("https://www.instagram.com/reel/SAMPLE008/", "SAMPLE008"),
        ("https://www.instagram.com/reel/test001/", "test001"),
        ("https://www.instagram.com/reel/fixture1/", "fixture1"),
        ("http://127.0.0.1:8000/reel/AAA111/", "AAA111"),
        ("http://localhost/reel/AAA111/", "AAA111"),
        ("https://www.instagram.com/explore/", ""),
        ("https://example.com/reel/AAA111/", "AAA111"),
        ("", ""),
    ],
)
def test_합성이거나_게시물이_아닌_주소는_대상이_아니다(url: str, media_id: str):
    assert is_real_instagram_target(url, media_id) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/reel/DcYTvd4O8qo/",
        "https://instagram.com/p/DQv9zb9EiA2/",
        "https://www.instagram.com/tv/DdBhlfLtg9B/",
    ],
)
def test_실제_instagram_게시물_주소는_대상이다(url: str):
    assert is_real_instagram_target(url) is True


# ===========================================================================
# 기다리는 상태 — 실패가 아니다
# ===========================================================================
@pytest.fixture()
def resume_config(config: Config, tmp_path) -> Config:
    raw = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in config.raw.items()
    }
    raw["paths"] = {**raw["paths"], "data_dir": str(tmp_path)}
    return Config(raw=raw, path=config.path, base_dir=tmp_path)


def _spend_ai(conn: sqlite3.Connection, count: int, *, date: str = "2026-10-07") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily_stats (stat_date, metric, value, updated_at) "
        "VALUES (?, 'claude_requests', ?, ?)",
        (date, count, date),
    )
    conn.commit()


def test_ai_한도를_다_썼으면_waiting_ai_budget이다(
    conn: sqlite3.Connection, resume_config: Config, tmp_path
):
    """한도 소진은 실패가 아니라 '아직 못 한다'이다."""
    _spend_ai(conn, 20)

    result = resume_canary(conn=conn, config=resume_config, now=NOW, allow_discovery=True)

    assert result.step == STEP_WAITING_BUDGET
    assert result.status == WAITING_AI_BUDGET
    assert result.ai_remaining == 0
    assert "한도" in result.reason
    assert load_state(state_path(tmp_path)).terminal is False  # 끝난 것이 아니다


def test_한도가_남아도_자격_후보가_없으면_waiting_eligible이다(
    conn: sqlite3.Connection, resume_config: Config
):
    _spend_ai(conn, 5)

    result = resume_canary(
        conn=conn, config=resume_config, now=NOW, allow_discovery=False
    )

    assert result.step == STEP_WAITING_ELIGIBLE
    assert result.status == WAITING_ELIGIBLE
    assert "낮추지 않는다" in result.reason


def test_기다리는_상태는_임계값을_바꾸지_않는다(
    conn: sqlite3.Connection, resume_config: Config
):
    before = resume_config.get("actions.require_score_for_like")

    resume_canary(conn=conn, config=resume_config, now=NOW, allow_discovery=False)

    assert resume_config.get("actions.require_score_for_like") == before == 75


def test_샘플_후보만_있으면_자격_후보로_세지_않는다(
    conn: sqlite3.Connection, resume_config: Config
):
    """점수가 아무리 높아도 열어 본 적 없는 합성 주소는 대상이 아니다."""
    _seed(conn, score=95.0, count=2)
    conn.execute(
        "UPDATE candidate_media SET canonical_url = "
        "'https://www.instagram.com/reel/SAMPLE00' || media_pk || '/', "
        "media_id = 'SAMPLE00' || media_pk"
    )
    conn.commit()
    _spend_ai(conn, 20)

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.eligible == 0
    assert result.step == STEP_WAITING_BUDGET


def test_실제_주소의_자격_후보는_센다(conn: sqlite3.Connection, resume_config: Config):
    _seed(conn, score=90.0, count=2)
    conn.execute(
        "UPDATE candidate_media SET canonical_url = "
        "'https://www.instagram.com/reel/Dc' || media_pk || 'YTvd4O8qo/', "
        "media_id = 'Dc' || media_pk || 'YTvd4O8qo'"
    )
    conn.commit()

    result = resume_canary(
        conn=conn, config=resume_config, now=NOW, allow_discovery=False, allow_live=False
    )

    assert result.eligible == 2


# ===========================================================================
# 리허설 → LIVE
# ===========================================================================
def test_자격_후보가_생기면_먼저_리허설을_한다(
    conn: sqlite3.Connection, resume_config: Config, tmp_path
):
    _seed(conn, score=90.0, count=1)
    conn.execute(
        "UPDATE candidate_media SET canonical_url = "
        "'https://www.instagram.com/reel/DcYTvd4O8qo/', media_id = 'DcYTvd4O8qo'"
    )
    conn.commit()

    result = resume_canary(
        conn=conn, config=resume_config, now=NOW, allow_discovery=False, allow_live=False
    )

    assert result.step == STEP_REHEARSED
    assert result.rehearsal_ok is True
    saved = load_state(state_path(tmp_path))
    assert saved.rehearsal_passed_at
    assert saved.status == READY_FOR_LIVE
    # 리허설만으로는 쓰기 창이 열리지 않는다.
    assert saved.live_started_at == ""


def test_리허설은_실제_쓰기를_만들지_않는다(
    conn: sqlite3.Connection, resume_config: Config
):
    _seed(conn, score=90.0, count=1)
    conn.execute(
        "UPDATE candidate_media SET canonical_url = "
        "'https://www.instagram.com/reel/DcYTvd4O8qo/', media_id = 'DcYTvd4O8qo'"
    )
    conn.commit()

    resume_canary(
        conn=conn, config=resume_config, now=NOW, allow_discovery=False, allow_live=False
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE dry_run = 0"
    ).fetchone()[0] == 0


# ===========================================================================
# 끝난 Canary / 상태 복구
# ===========================================================================
def test_종료된_canary는_resume해도_아무것도_하지_않는다(
    conn: sqlite3.Connection, resume_config: Config, tmp_path
):
    state = arm(CanaryState(), now=NOW)
    state.stop(COMPLETED, "total_reached", now=NOW)
    save_state(state_path(tmp_path), state)

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_NOOP
    assert result.status == COMPLETED


def test_안전중단된_canary도_resume해도_아무것도_하지_않는다(
    conn: sqlite3.Connection, resume_config: Config, tmp_path
):
    state = arm(CanaryState(), now=NOW)
    state.stop(STOPPED_SAFETY, "platform_warning", now=NOW)
    save_state(state_path(tmp_path), state)

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_NOOP
    assert result.status == STOPPED_SAFETY


def test_상태파일이_없는데_실제_like기록이_있으면_막는다(
    conn: sqlite3.Connection, resume_config: Config
):
    """새 Canary를 여는 것은 한도를 처음부터 다시 쓰겠다는 뜻이다(§42)."""
    _seed(conn, count=1)
    conn.execute(
        """INSERT INTO interactions
           (media_pk, creator_id, action_type, executor, dry_run, success, executed_at)
           VALUES (1, 1, 'LIKE', 'browser', 0, 1, '2026-10-01T00:00:00+00:00')"""
    )
    conn.commit()

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_BLOCKED
    assert result.status == STOPPED_SAFETY
    assert "처음부터 다시" in result.reason


def test_상태파일이_깨졌으면_처음부터_시작하지_않는다(
    conn: sqlite3.Connection, resume_config: Config, tmp_path
):
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 깨진", encoding="utf-8")

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_NOOP  # terminal 로 읽혀 아무 것도 하지 않는다
    assert "state_file_unreadable" in result.reason


# ===========================================================================
# 날짜 경계 — 고정 날짜가 아니라 상대 시간으로 본다 (§40)
# ===========================================================================
def test_날이_바뀌면_ai_한도가_새_창으로_넘어간다(
    conn: sqlite3.Connection, resume_config: Config
):
    """어제 다 썼다고 오늘까지 막히면 Canary가 영원히 못 깨어난다."""
    yesterday = (NOW - timedelta(days=1) + timedelta(hours=9)).date().isoformat()
    _spend_ai(conn, 20, date=yesterday)

    result = resume_canary(
        conn=conn, config=resume_config, now=NOW, allow_discovery=False
    )

    assert result.ai_used == 0  # 오늘 쓴 것은 없다
    assert result.ai_remaining == 20
    assert result.step == STEP_WAITING_ELIGIBLE  # 한도가 아니라 후보가 없어서 기다린다


def test_오늘_일부만_썼으면_남은_만큼만_쓴다(
    conn: sqlite3.Connection, resume_config: Config
):
    _spend_ai(conn, 17)

    result = resume_canary(
        conn=conn, config=resume_config, now=NOW, allow_discovery=False
    )

    assert result.ai_used == 17
    assert result.ai_remaining == 3


# ===========================================================================
# 반복 호출 안전 (§19)
# ===========================================================================
def test_여러_번_불러도_기다리는_상태를_넘어서지_않는다(
    conn: sqlite3.Connection, resume_config: Config
):
    _spend_ai(conn, 20)

    for _ in range(5):
        result = resume_canary(
            conn=conn, config=resume_config, now=NOW, allow_discovery=True
        )

    assert result.step == STEP_WAITING_BUDGET
    assert conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE dry_run = 0"
    ).fetchone()[0] == 0


def test_resume은_사람_feedback을_만들지_않는다(
    conn: sqlite3.Connection, resume_config: Config
):
    """Agent가 누른 것은 사람 판단이 아니다(§33)."""
    before = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]

    resume_canary(conn=conn, config=resume_config, now=NOW, allow_discovery=False)

    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == before


def test_DB_연결이_실패해도_lock은_해제된다(resume_config: Config, tmp_path, monkeypatch):
    """lock을 잡은 뒤 conn 생성이 실패하면 finally의 release에 도달해야 한다(L2).

    release되지 않으면 다음 실행이 '소유자 없음 … 비정상 종료로 보고 인수' 경고를 거짓으로 낸다.
    """
    import json

    from targeting_agent.canary import resume as resume_module

    def broken(_path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(resume_module, "get_connection", broken)

    result = resume_canary(config=resume_config, now=NOW, allow_discovery=False)

    assert result.step == STEP_BLOCKED and "OperationalError" in result.reason
    lock_path = tmp_path / "data" / ".canary_resume.lock"
    assert "released_at" in json.loads(lock_path.read_text(encoding="utf-8"))


# ===========================================================================
# Discovery 결과 상태 — 운영 장애를 "후보 없음"으로 바꾸지 않는다 (P1-A)
# ===========================================================================
REAL_URL = "https://www.instagram.com/reel/DcYTvd4O8qo/"


class _FakeDiscovery:
    """run_browser_discovery 대역. 실제 브라우저·Claude를 쓰지 않는다."""

    def __init__(self, result, *, side_effect=None) -> None:
        self.result = result
        self.side_effect = side_effect
        self.calls = 0

    def __call__(self, config, conn, **kwargs):
        self.calls += 1
        if self.side_effect is not None:
            self.side_effect(conn)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _install(monkeypatch, result, *, side_effect=None) -> _FakeDiscovery:
    fake = _FakeDiscovery(result, side_effect=side_effect)
    monkeypatch.setattr(browser_runner, "run_browser_discovery", fake)
    return fake


def _discovery_result(status: str, stop_reason=None, message: str = "") -> BrowserDiscoveryResult:
    return BrowserDiscoveryResult(status=status, stop_reason=stop_reason, message=message)


def _seed_real_candidate(conn: sqlite3.Connection) -> None:
    _seed(conn, score=90.0, count=1)
    conn.execute(
        "UPDATE candidate_media SET canonical_url = ?, media_id = 'DcYTvd4O8qo'", (REAL_URL,)
    )
    conn.commit()


def test_discovery가_성공하고_자격_후보가_생기면_다음_단계로_간다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    _spend_ai(conn, 5)
    fake = _install(
        monkeypatch, _discovery_result(browser_runner.STATUS_SUCCESS),
        side_effect=_seed_real_candidate,
    )

    result = resume_canary(conn=conn, config=resume_config, now=NOW, allow_live=False)

    assert fake.calls == 1
    assert result.step == STEP_REHEARSED
    assert result.eligible == 1
    assert result.source_status == "SUCCESS"
    assert load_state(state_path(tmp_path)).discovery_runs == 1


@pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL", "EMPTY"])
def test_discovery가_정상_수행됐는데_후보가_없으면_waiting_eligible이다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path, status: str
):
    """이 경우에만 '후보 없음'이 맞다."""
    _spend_ai(conn, 5)
    _install(monkeypatch, _discovery_result(status))

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_WAITING_ELIGIBLE
    assert result.status == WAITING_ELIGIBLE
    assert result.source_status == status
    assert "낮추지 않는다" in result.reason
    saved = load_state(state_path(tmp_path))
    assert saved.terminal is False
    assert saved.discovery_runs == 1


def test_최고점_문구는_db_누적_값임을_밝힌다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    """best는 DB 전체 집계다 — 이번 실행 결과처럼 읽히면 점수 부족으로 오해한다."""
    _spend_ai(conn, 5)
    _install(monkeypatch, _discovery_result("EMPTY"))

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert "DB 누적" in result.reason
    assert "이번 실행 결과가 아님" in result.reason


def test_discovery_lock_경쟁은_waiting_next_run이지_후보없음이_아니다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    _spend_ai(conn, 5)
    fake = _install(
        monkeypatch, _discovery_result("LOCKED", message="다른 Browser Discovery가 진행 중")
    )

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert fake.calls == 1
    assert result.status == WAITING_NEXT_RUN
    assert result.status != WAITING_ELIGIBLE
    assert result.step != STEP_WAITING_ELIGIBLE
    # lock 경쟁은 정상적인 일시 상태다 — exit 1 이면 8시간마다 Task Scheduler 에 실패로 보인다.
    assert result.step == STEP_NOOP and result.exit_code == 0
    assert "lock 경쟁" in result.reason
    assert "후보 없음" not in result.reason
    assert result.source_status == "LOCKED"
    saved = load_state(state_path(tmp_path))
    assert saved.terminal is False  # 일시적이므로 끝내지 않는다
    assert saved.status == WAITING_NEXT_RUN
    assert "lock 경쟁" in saved.last_waiting_reason
    # Discovery가 일어나지 않았으므로 세지 않는다.
    assert saved.discovery_runs == 0


def test_discovery_disabled도_waiting_next_run이다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    """방어 분기 검증이다. Resume 경로에서는 DISABLED 에 도달할 수 없다
    (`_run_discovery` 가 enabled=True 를 강제하므로 fake 가 직접 돌려줄 때만 나온다).
    이 테스트의 통과를 실제 도달 가능성의 근거로 쓰지 말 것."""
    _spend_ai(conn, 5)
    _install(monkeypatch, _discovery_result("DISABLED"))

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.status == WAITING_NEXT_RUN
    assert result.step == STEP_NOOP and result.exit_code == 0
    assert result.source_status == "DISABLED"
    assert "비활성화" in result.reason
    saved = load_state(state_path(tmp_path))
    assert saved.terminal is False
    assert saved.discovery_runs == 0


@pytest.mark.parametrize(
    "stop_reason, expected",
    [
        ("LOGIN_REQUIRED", STOPPED_LOGIN),
        ("CHALLENGE", STOPPED_CHALLENGE),
        ("PLATFORM_WARNING", STOPPED_WARNING),
        ("UNKNOWN", STOPPED_SAFETY),
        (None, STOPPED_SAFETY),
    ],
)
def test_세션_중단은_기존_매핑으로_종료_상태가_된다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path,
    stop_reason, expected: str,
):
    _spend_ai(conn, 5)
    _install(monkeypatch, _discovery_result("SESSION_STOPPED", stop_reason=stop_reason))

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.status == expected
    assert result.step == STEP_BLOCKED
    assert result.step != STEP_WAITING_ELIGIBLE
    assert result.source_status == "SESSION_STOPPED"
    assert result.source_stop_reason == (stop_reason or "")
    saved = load_state(state_path(tmp_path))
    assert saved.status == expected
    assert saved.terminal is True
    assert "discovery_session_stopped" in saved.stop_reason


def test_세션_중단_뒤_다시_resume해도_discovery를_다시_열지_않는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    """S1 — 플랫폼 이상 신호 뒤에는 재시도하지 않는다. 허용 방향은 STOP뿐이다."""
    _spend_ai(conn, 5)
    fake = _install(
        monkeypatch, _discovery_result("SESSION_STOPPED", stop_reason="CHALLENGE")
    )

    first = resume_canary(conn=conn, config=resume_config, now=NOW)
    second = resume_canary(conn=conn, config=resume_config, now=NOW + timedelta(hours=1))
    third = resume_canary(conn=conn, config=resume_config, now=NOW + timedelta(days=1))

    assert first.status == STOPPED_CHALLENGE
    assert fake.calls == 1  # 브라우저 Discovery는 한 번뿐
    assert second.step == STEP_NOOP and second.status == STOPPED_CHALLENGE
    assert third.step == STEP_NOOP and third.status == STOPPED_CHALLENGE


def test_discovery_실패는_후보없음으로_숨지_않고_일시_실패로_남는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    _spend_ai(conn, 5)
    fake = _install(monkeypatch, _discovery_result("FAILED", message="검색어가 없습니다"))

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_BLOCKED
    assert result.step != STEP_WAITING_ELIGIBLE
    assert result.exit_code == 1
    # Discovery의 FAILED를 Canary의 FAILED(영구 종료)로 복사하지 않는다.
    assert result.status == WAITING_NEXT_RUN
    assert result.source_status == "FAILED"
    assert "후보 없음이 아니다" in result.reason
    saved = load_state(state_path(tmp_path))
    assert saved.terminal is False
    # 일시적 실패이므로 다음 Resume은 다시 시도할 수 있다.
    resume_canary(conn=conn, config=resume_config, now=NOW + timedelta(hours=1))
    assert fake.calls == 2


def test_discovery_예외도_후보없음으로_숨지_않는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    _spend_ai(conn, 5)
    _install(monkeypatch, RuntimeError("boom"))

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_BLOCKED
    assert result.status == WAITING_NEXT_RUN
    assert result.source_status == "FAILED"
    assert any("RuntimeError" in message for message in result.messages)
    assert load_state(state_path(tmp_path)).terminal is False


def test_상태를_알_수_없는_discovery_결과는_성공으로_읽지_않는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    """결과 객체의 status 기본값은 SUCCESS다 — 값이 빠졌다고 성공이 되면 안 된다."""
    _spend_ai(conn, 5)
    _install(monkeypatch, object())

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.source_status == "FAILED"
    assert result.step == STEP_BLOCKED


def test_ai_예산이_없으면_discovery를_열지_않고_waiting_ai_budget이다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    _spend_ai(conn, 20)
    fake = _install(monkeypatch, _discovery_result("SUCCESS"))

    result = resume_canary(conn=conn, config=resume_config, now=NOW, allow_discovery=True)

    assert fake.calls == 0
    assert result.step == STEP_WAITING_BUDGET
    assert result.status == WAITING_AI_BUDGET


def test_discovery_도중_예산이_소진되면_waiting_ai_budget이다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    _spend_ai(conn, 5)
    _install(
        monkeypatch, _discovery_result("SUCCESS"),
        side_effect=lambda c: _spend_ai(c, 20),
    )

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.step == STEP_WAITING_BUDGET
    assert result.status == WAITING_AI_BUDGET


def test_discovery_원래_상태와_중단_사유가_출력에_보인다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    _spend_ai(conn, 5)
    _install(
        monkeypatch, _discovery_result("SESSION_STOPPED", stop_reason="LOGIN_REQUIRED")
    )

    result = resume_canary(conn=conn, config=resume_config, now=NOW)
    text = format_resume(result)

    assert "SESSION_STOPPED (LOGIN_REQUIRED)" in text
    assert "STOPPED_LOGIN" in text  # Canary 상태는 별도로 보인다
    assert result.as_rows()["Discovery 상태"] == "SESSION_STOPPED (LOGIN_REQUIRED)"


def test_discovery_임계값과_한도는_상태에_따라_바뀌지_않는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    _spend_ai(conn, 5)
    before = (
        resume_config.get("actions.require_score_for_like"),
        resume_config.get("ai.daily_request_limit"),
    )
    for status in ("LOCKED", "FAILED", "EMPTY"):
        _install(monkeypatch, _discovery_result(status))
        resume_canary(conn=conn, config=resume_config, now=NOW)

    assert before == (
        resume_config.get("actions.require_score_for_like"),
        resume_config.get("ai.daily_request_limit"),
    )


# ===========================================================================
# 종료 상태의 복구 안내 — 실제 복구 수단과 일치해야 한다 (F1)
# ===========================================================================
def _stopped(conn, resume_config, monkeypatch, stop_reason, message=""):
    _spend_ai(conn, 5)
    fake = _install(
        monkeypatch,
        _discovery_result("SESSION_STOPPED", stop_reason=stop_reason, message=message),
    )
    return fake, resume_canary(conn=conn, config=resume_config, now=NOW)


def test_세션중단_안내는_재로그인으로_재개된다고_말하지_않는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    """Discovery 원문('로그인한 뒤 다시 시도하세요')은 Canary 층에서는 거짓이다."""
    _, result = _stopped(
        conn, resume_config, monkeypatch, "LOGIN_REQUIRED",
        message="Instagram 로그인이 필요합니다. run_instagram_session.bat 을 실행해 "
        "직접 로그인한 뒤 다시 시도하세요.",
    )

    text = "\n".join(result.messages) + format_resume(result)
    assert "다시 시도하세요" not in text
    assert "run_instagram_session" not in text
    assert "종료 상태(STOPPED_LOGIN)" in text
    assert "재로그인·재실행만으로는 재개되지 않는다" in text
    assert str(state_path(tmp_path)) in text  # 실제 상태 파일 경로
    assert "WAITING_NEXT_RUN" in text and "stop_reason" in text and "completed_at" in text
    assert "CLI" in text  # 재개 명령이 없다는 사실


def test_종료_뒤_다시_resume해도_복구_안내가_다시_보인다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    _stopped(conn, resume_config, monkeypatch, "CHALLENGE")

    second = resume_canary(conn=conn, config=resume_config, now=NOW + timedelta(hours=9))

    assert second.step == STEP_NOOP and second.status == STOPPED_CHALLENGE
    assert any(str(state_path(tmp_path)) in m for m in second.messages)


def test_안내한_수동_절차를_따르면_실제로_재개된다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    """안내가 실제 해제 수단과 일치하는지 — 안내대로 상태 파일을 고치면 Discovery 가 다시 열린다."""
    import json

    fake, _ = _stopped(conn, resume_config, monkeypatch, "LOGIN_REQUIRED")
    path = state_path(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update({"status": "WAITING_NEXT_RUN", "stop_reason": "", "completed_at": ""})
    path.write_text(json.dumps(raw), encoding="utf-8")

    again = resume_canary(conn=conn, config=resume_config, now=NOW + timedelta(hours=9))

    assert fake.calls == 2
    assert again.step != STEP_NOOP


def test_세션_상태_판독불가는_플랫폼_신호가_확정된_것이_아님을_밝히고_종료를_유지한다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    """UNKNOWN 은 의도적으로 TERMINAL 이다(판독 불가 상태에서 브라우저를 다시 열지 않는다)."""
    fake, result = _stopped(conn, resume_config, monkeypatch, "UNKNOWN")

    assert result.status == STOPPED_SAFETY
    assert load_state(state_path(tmp_path)).terminal is True
    assert "판독하지 못했다" in result.reason
    assert "플랫폼 신호가 확정된 것은 아니다" in result.reason
    assert any("판독하지 못했다" in m for m in result.messages)
    assert any("재개되지 않는다" in m for m in result.messages)
    resume_canary(conn=conn, config=resume_config, now=NOW + timedelta(hours=9))
    assert fake.calls == 1  # 그래도 다시 열지 않는다


def test_stop_reason이_enum이어도_값_문자열로_보존된다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    from targeting_agent.discovery.browser_models import SessionState

    _, result = _stopped(conn, resume_config, monkeypatch, SessionState.CHALLENGE)

    assert result.source_stop_reason == "CHALLENGE"
    assert result.status == STOPPED_CHALLENGE
    assert "SessionState" not in format_resume(result)


# ===========================================================================
# 이미 자격을 얻은 후보는 버리지 않는다 (F2)
# ===========================================================================
@pytest.mark.parametrize("status", ["LOCKED", "FAILED"])
def test_lock이나_실패여도_자격_후보가_이미_있으면_진행한다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path, status: str
):
    """동시 실행이 같은 DB 에 자격 후보를 커밋했을 수 있다 — 8시간을 버리지 않는다."""
    _spend_ai(conn, 5)
    _install(
        monkeypatch, _discovery_result(status, message="x"), side_effect=_seed_real_candidate
    )

    result = resume_canary(conn=conn, config=resume_config, now=NOW, allow_live=False)

    assert result.step == STEP_REHEARSED
    assert result.eligible == 1
    assert result.source_status == status  # 원래 상태는 그대로 남는다
    assert any(status in m and "기존 후보로 진행" in m for m in result.messages)
    assert load_state(state_path(tmp_path)).rehearsal_passed_at


def test_세션중단이면_자격_후보가_있어도_진행하지_않는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, tmp_path
):
    """플랫폼 이상 신호에서는 후보가 있어도 STOP 이다."""
    _spend_ai(conn, 5)
    _install(
        monkeypatch, _discovery_result("SESSION_STOPPED", stop_reason="PLATFORM_WARNING"),
        side_effect=_seed_real_candidate,
    )

    result = resume_canary(conn=conn, config=resume_config, now=NOW, allow_live=False)

    assert result.eligible == 1
    assert result.status == STOPPED_WARNING
    assert result.rehearsal_ok is None
    saved = load_state(state_path(tmp_path))
    assert saved.terminal is True and saved.rehearsal_passed_at == ""


# ===========================================================================
# 예산 정보 보존 (F6) / 출력 형태 (F4)
# ===========================================================================
def test_discovery_실패와_예산_소진이_겹치면_사유에_예산도_남는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch
):
    _spend_ai(conn, 5)
    _install(
        monkeypatch, _discovery_result("FAILED"), side_effect=lambda c: _spend_ai(c, 20)
    )

    result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert result.status == WAITING_NEXT_RUN
    assert "Discovery 실행 실패" in result.reason
    assert "20/20" in result.reason and "소진" in result.reason


def test_discovery_상태_표시는_빈_사유나_빈_상태를_이상하게_찍지_않는다():
    from targeting_agent.canary.resume import ResumeResult

    assert ResumeResult(source_status="LOCKED").as_rows()["Discovery 상태"] == "LOCKED"
    assert ResumeResult().as_rows()["Discovery 상태"] == "-"
    assert (
        ResumeResult(source_status="SESSION_STOPPED", source_stop_reason="CHALLENGE")
        .as_rows()["Discovery 상태"]
        == "SESSION_STOPPED (CHALLENGE)"
    )
    # 상태 없이 사유만 있어도 괄호만 찍히지 않는다.
    assert "()" not in ResumeResult(source_status="LOCKED").as_rows()["Discovery 상태"]


def test_분기가_없는_새_status는_경고를_남기고_failed로_처리된다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, caplog
):
    """_KNOWN_STATUSES 는 browser_runner 의 복사본이다 — drift 는 경고로 드러나야 한다."""
    import logging

    _spend_ai(conn, 5)
    _install(monkeypatch, _discovery_result("RATE_LIMITED"))

    with caplog.at_level(logging.WARNING):
        result = resume_canary(conn=conn, config=resume_config, now=NOW)

    assert "알 수 없는 Discovery 상태" in caplog.text
    assert "RATE_LIMITED" in caplog.text
    assert result.source_status == "FAILED"
    assert result.step == STEP_BLOCKED
    assert result.status == WAITING_NEXT_RUN


def test_알려진_status는_알_수_없음_경고를_내지_않는다(
    conn: sqlite3.Connection, resume_config: Config, monkeypatch, caplog
):
    import logging

    _spend_ai(conn, 5)
    _install(monkeypatch, _discovery_result("LOCKED"))

    with caplog.at_level(logging.WARNING):
        resume_canary(conn=conn, config=resume_config, now=NOW)

    assert "알 수 없는 Discovery 상태" not in caplog.text
