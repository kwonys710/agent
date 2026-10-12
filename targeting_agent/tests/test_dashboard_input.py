"""Phase 12B — Dashboard Candidate Input + 즉시 처리 테스트.

실제 Claude CLI를 호출하지 않는다(FakeRunner 기반 Intelligence 주입).
"""
from __future__ import annotations

import pytest

from targeting_agent.ai.claude_runner import RunnerResult
from targeting_agent.ai.intelligence import DAILY_METRIC, ClaudeIntelligence
from targeting_agent.ai.schema import FailureReason, TargetingAnalysis
from targeting_agent.core.config import Config
from targeting_agent.core.database import bump_stat, today_str
from targeting_agent.core.models import MediaStatus
from targeting_agent.dashboard.app import Page, render
from targeting_agent.dashboard.service import SOURCE_DASHBOARD, DashboardService
from targeting_agent.pipeline import CandidateProcessor

URL = "https://www.instagram.com/reel/ABC123/"
CAPTION = "퇴근 후 저녁 먹으러 왔습니다 #직장인 #퇴근"
PAYLOAD = {
    "summary": "퇴근 후 저녁 식사",
    "primary_topic": "퇴근",
    "topics": ["퇴근", "직장생활"],
    "mood": "편안",
    "language": "ko",
    "relevance_score": 0.88,
    "relevance_reason": "직장인 퇴근 맥락과 맞는다",
    "comment_candidates": [
        "퇴근하고 이 메뉴는 못 참죠",
        "이런 한 끼가 진짜 퇴근 후 행복이죠",
        "오늘 저녁 메뉴가 정해졌네요",
    ],
}


class FakeRunner:
    """실제 CLI 대신 정해진 결과를 돌려준다."""

    def __init__(self, result: RunnerResult | None = None, available: bool = True):
        self.result = result or RunnerResult(
            ok=True, analysis=TargetingAnalysis.model_validate(PAYLOAD), cost_usd=0.02, num_turns=1
        )
        self.calls: list[str] = []
        self._available = available

    def available(self) -> bool:
        return self._available

    def run(self, prompt: str) -> RunnerResult:
        self.calls.append(prompt)
        return self.result


def _ai_config(config: Config, **overrides) -> Config:
    ai = {**config.raw.get("ai", {}), "provider": "claude_code", **overrides}
    return Config(raw={**config.raw, "ai": ai}, path=config.path, base_dir=config.base_dir)


def _service(conn, config, runner: FakeRunner | None = None) -> tuple[DashboardService, FakeRunner]:
    """Intelligence에 FakeRunner를 주입한 Dashboard 서비스."""
    runner = runner or FakeRunner()
    ai_config = _ai_config(config, daily_request_limit=20, max_candidates_per_run=10)
    service = DashboardService(conn, ai_config)

    def factory():
        from targeting_agent.analysis.profile_analyzer import load_profile

        profile = load_profile(ai_config.profile_path, conn)
        return CandidateProcessor(
            ai_config,
            conn,
            profile=profile,
            intelligence=ClaudeIntelligence(ai_config, profile, runner=runner),
            seed=1,
        )

    service._default_processor = factory  # type: ignore[assignment]
    return service, runner


def _add(service, **kwargs):
    return service.add_candidate(kwargs.pop("url", URL), **kwargs)


def _count(conn, sql: str, *params) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# --- 1~5. 입력 / 검증 / 중복 ------------------------------------------------
def test_add_url_registers_candidate(conn, config) -> None:
    service, runner = _service(conn, config)
    result = _add(service)

    # URL만 등록하면 오류가 아니라 '정보 부족' 상태로 안내한다
    assert result.status == "NEEDS_ENRICHMENT" and result.media_pk
    row = conn.execute("SELECT canonical_url, source, status FROM candidate_media").fetchone()
    assert row["canonical_url"] == URL
    assert row["source"] == SOURCE_DASHBOARD
    assert runner.calls == []                      # '추가만'은 Claude를 부르지 않는다


def test_url_query_normalized(conn, config) -> None:
    service, _ = _service(conn, config)
    _add(service, url="https://instagram.com/reel/ABC123?utm_source=ig_web&igshid=x")

    assert conn.execute("SELECT canonical_url FROM candidate_media").fetchone()[0] == URL


@pytest.mark.parametrize(
    "bad_url", ["", "   ", "not-a-url", "https://youtube.com/watch?v=1",
                "https://www.instagram.com/stories/user/1/"]
)
def test_invalid_url_rejected(conn, config, bad_url: str) -> None:
    service, _ = _service(conn, config)
    result = _add(service, url=bad_url)

    assert result.status == "INVALID"
    assert _count(conn, "SELECT COUNT(*) FROM candidate_media") == 0


def test_oversized_input_rejected(conn, config) -> None:
    service, _ = _service(conn, config)
    result = _add(service, caption="가" * 2001)

    assert result.status == "INVALID" and "너무 깁니다" in result.message
    assert _count(conn, "SELECT COUNT(*) FROM candidate_media") == 0


def test_duplicate_url_keeps_single_candidate(conn, config) -> None:
    service, _ = _service(conn, config)
    first = _add(service)
    second = _add(service, url="https://www.instagram.com/reel/ABC123/?utm_source=y")

    assert second.status == "DUPLICATE"
    assert second.media_pk == first.media_pk        # 기존 후보로 이동 가능
    assert _count(conn, "SELECT COUNT(*) FROM candidate_media") == 1


# --- 6~9. 추가만 / 추가 후 분석 ---------------------------------------------
def test_url_only_is_needs_enrichment_without_claude(conn, config) -> None:
    service, runner = _service(conn, config)
    result = _add(service, analyze=True)

    assert result.status == "NEEDS_ENRICHMENT"
    assert runner.calls == []                       # URL만으로 Claude에 보내지 않는다
    assert conn.execute("SELECT status FROM candidate_media").fetchone()[0] == (
        MediaStatus.NEEDS_ENRICHMENT.value
    )


def test_add_only_with_caption_reports_added(conn, config) -> None:
    service, runner = _service(conn, config)
    result = _add(service, username="creator_a", caption=CAPTION)

    assert result.status == "ADDED"
    assert runner.calls == []                       # '추가만'은 분석하지 않는다
    assert conn.execute("SELECT status FROM candidate_media").fetchone()[0] == MediaStatus.NEW.value


def test_caption_enables_analysis(conn, config) -> None:
    service, runner = _service(conn, config)
    result = _add(service, username="creator_a", caption=CAPTION, note="직장인 브이로그 후보", analyze=True)

    assert result.status == "ANALYZED"
    assert result.analysis_source == "claude"
    assert len(runner.calls) == 1                   # 후보 1건당 Claude 1회
    assert result.target_score and result.target_score > 0
    assert result.comment_count >= 1
    assert conn.execute("SELECT status FROM candidate_media").fetchone()[0] == (
        MediaStatus.SCORED.value
    )


def test_note_saved_without_candidate_schema_change(conn, config) -> None:
    service, _ = _service(conn, config)
    _add(service, caption=CAPTION, note="직장인 브이로그 후보")

    assert conn.execute("SELECT note FROM import_events").fetchone()[0] == "직장인 브이로그 후보"


def test_analysis_uses_claude_comments(conn, config) -> None:
    service, _ = _service(conn, config)
    _add(service, caption=CAPTION, analyze=True)

    texts = [r[0] for r in conn.execute("SELECT text FROM comment_drafts WHERE quality_ok = 1")]
    assert any("퇴근" in t for t in texts)
    generators = {r[0] for r in conn.execute("SELECT DISTINCT generator FROM comment_drafts")}
    assert "claude_code" in generators


# --- 10~11. 캐시 / 중복 제출 -------------------------------------------------
def test_repeated_analysis_hits_cache(conn, config) -> None:
    service, runner = _service(conn, config)
    first = _add(service, caption=CAPTION, analyze=True)
    second = _add(service, caption=CAPTION, analyze=True)

    assert first.analysis_source == "claude"
    assert second.analysis_source == "cache"
    assert len(runner.calls) == 1


def test_ten_submissions_one_candidate_one_claude_call(conn, config) -> None:
    """핵심 조건: 같은 입력 10회 → Candidate 1건, Claude 최대 1회."""
    service, runner = _service(conn, config)
    results = [
        _add(service, username="creator_a", caption=CAPTION, analyze=True) for _ in range(10)
    ]

    assert _count(conn, "SELECT COUNT(*) FROM candidate_media") == 1
    assert len(runner.calls) == 1
    assert [r.analysis_source for r in results[1:]] == ["cache"] * 9
    assert _count(conn, "SELECT COUNT(*) FROM ai_analysis_cache") == 1


# --- 12~15. 실패 / 한도 ------------------------------------------------------
def test_claude_unavailable_falls_back_to_heuristic(conn, config) -> None:
    service, runner = _service(conn, config, FakeRunner(available=False))
    result = _add(service, caption=CAPTION, analyze=True)

    assert result.status == "FALLBACK_ANALYZED"
    assert result.analysis_source == "heuristic"
    assert runner.calls == []
    assert result.target_score is not None          # 점수는 계산된다


def test_claude_timeout_falls_back(conn, config) -> None:
    timeout = RunnerResult(ok=False, failure=FailureReason.TIMEOUT, detail="120s 초과")
    service, runner = _service(conn, config, FakeRunner(result=timeout))
    result = _add(service, caption=CAPTION, analyze=True)

    assert result.status == "FALLBACK_ANALYZED"
    assert len(runner.calls) == 1                   # 같은 요청을 재시도하지 않는다
    assert _count(conn, "SELECT COUNT(*) FROM ai_analysis_cache") == 0


def test_daily_limit_blocks_claude(conn, config) -> None:
    bump_stat(conn, today_str(9), DAILY_METRIC, 20)
    conn.commit()

    service, runner = _service(conn, config)
    result = _add(service, caption=CAPTION, analyze=True)

    assert runner.calls == []
    assert result.status == "FALLBACK_ANALYZED"


def test_max_candidates_per_run_guard_kept(conn, config) -> None:
    runner = FakeRunner()
    ai_config = _ai_config(config, max_candidates_per_run=1, daily_request_limit=20)
    from targeting_agent.analysis.profile_analyzer import load_profile

    profile = load_profile(ai_config.profile_path, conn)
    intelligence = ClaudeIntelligence(ai_config, profile, runner=runner)
    service = DashboardService(conn, ai_config)
    service._default_processor = lambda: CandidateProcessor(  # type: ignore[assignment]
        ai_config, conn, profile=profile, intelligence=intelligence, seed=1
    )

    _add(service, url="https://www.instagram.com/reel/AAA/", caption=CAPTION, analyze=True)
    _add(service, url="https://www.instagram.com/reel/BBB/", caption="주말 카페 #카페", analyze=True)

    assert len(runner.calls) == 1
    assert intelligence.usage.limited == 1


# --- 16~18. Review 연결 / Approval Gate --------------------------------------
def test_analyzed_candidate_visible_in_review(conn, config) -> None:
    service, _ = _service(conn, config)
    result = _add(service, username="creator_a", caption=CAPTION, analyze=True)

    html = render(conn, config, Page(tab="review", media_pk=result.media_pk), token="t")
    assert "퇴근" in html and "0.88" in html
    assert "새 Candidate 추가" not in html          # 상세 화면에는 입력 폼이 없다

    list_html = render(conn, config, Page(tab="review", filters={}), token="t")
    assert "새 Candidate 추가" in list_html
    assert "추가 후 분석" in list_html and "추가만" in list_html


def test_add_and_analyze_does_not_create_or_approve_actions(conn, config) -> None:
    """분석은 추천까지만 한다. 승인은 Review에서 운영자가 한다(Approval Gate 유지)."""
    service, _ = _service(conn, config)
    _add(service, username="creator_a", caption=CAPTION, analyze=True)

    assert _count(conn, "SELECT COUNT(*) FROM action_queue") == 0
    from targeting_agent.core.database import fetch_executable_actions

    assert fetch_executable_actions(conn) == []


def test_approval_after_add_keeps_gate(conn, config) -> None:
    from targeting_agent.core.models import ActionType

    service, _ = _service(conn, config)
    result = _add(service, username="creator_a", caption=CAPTION, analyze=True)
    service.approve(result.media_pk, [ActionType.LIKE])

    row = conn.execute("SELECT status, approved_at FROM action_queue").fetchone()
    assert row["status"] == "PENDING" and row["approved_at"]   # 명시적 승인 후에만 실행 대상
