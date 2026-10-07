"""Phase 13 — Claude Code Runtime 기반 AI 계층 테스트.

**실제 Claude CLI를 호출하지 않는다.** FakeRunner로 응답을 주입해 검증한다.
실제 런타임 확인은 scripts/probe_claude_runtime.py 로 별도 수행한다.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from targeting_agent.ai.cache import cache_key, get_cached, normalized_candidate_input
from targeting_agent.ai.claude_runner import ClaudeRunner
from targeting_agent.ai.intelligence import DAILY_METRIC, ClaudeIntelligence
from targeting_agent.ai.schema import FailureReason, TargetingAnalysis, parse_analysis
from targeting_agent.analysis.content_analyzer import HeuristicAnalyzer, merge_claude_analysis
from targeting_agent.analysis.scorer import TargetScorer
from targeting_agent.comments.duplicate_filter import CommentDuplicateFilter
from targeting_agent.comments.generator import CommentGenerator
from targeting_agent.core.config import Config
from targeting_agent.core.database import bump_stat, get_stats, today_str

MEDIA = {
    "media_pk": 1,
    "media_id": "M1",
    "username": "commute_story",
    "caption": "출근길 버스에서 보는 아침 하늘 #출근 #직장인일상",
    "hashtags": '["출근", "직장인일상"]',
    "media_type": "REEL",
    "followers": 4400,
    "like_count": 640,
    "comment_count": 38,
    "is_private": 0,
}

GOOD_PAYLOAD = {
    "summary": "출근길 버스에서 본 아침 하늘",
    "primary_topic": "출근",
    "topics": ["출근", "직장인일상"],
    "mood": "차분",
    "language": "ko",
    "relevance_score": 0.9,
    "relevance_reason": "직장인 출근 맥락이 그대로 겹친다",
    "comment_candidates": [
        "버스 창밖 아침 하늘 색감이 예쁘네요",
        "출근길에 하늘 보는 여유 저도 챙겨야겠어요",
        "월요일 출근길인데 하늘이 맑아 다행이에요",
    ],
}


class FakeRunner:
    """ClaudeRunner 대체. 실제 CLI를 부르지 않고 미리 정한 결과를 돌려준다."""

    def __init__(self, results=None, available: bool = True):
        from targeting_agent.ai.claude_runner import RunnerResult

        self._RunnerResult = RunnerResult
        self.results = results if results is not None else [self.success()]
        self.calls: list[str] = []
        self._available = available

    def success(self, payload=None):
        from targeting_agent.ai.claude_runner import RunnerResult

        analysis = TargetingAnalysis.model_validate(payload or GOOD_PAYLOAD)
        return RunnerResult(ok=True, analysis=analysis, cost_usd=0.02, num_turns=1)

    def failure(self, reason: FailureReason, detail: str = ""):
        from targeting_agent.ai.claude_runner import RunnerResult

        return self._RunnerResult(ok=False, failure=reason, detail=detail)

    def available(self) -> bool:
        return self._available

    def run(self, prompt: str):
        self.calls.append(prompt)
        if not self.results:
            return self.success()
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def _config(config: Config, **ai_overrides) -> Config:
    ai = {**config.raw.get("ai", {}), "provider": "claude_code", **ai_overrides}
    return Config(raw={**config.raw, "ai": ai}, path=config.path, base_dir=config.base_dir)


def _intelligence(config: Config, profile, runner: FakeRunner, **overrides) -> ClaudeIntelligence:
    return ClaudeIntelligence(_config(config, **overrides), profile, runner=runner)


# --- 1. CLI 존재 여부 ------------------------------------------------------
def test_runner_reports_missing_cli(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = ClaudeRunner()
    assert runner.available() is False

    result = runner.run("prompt")
    assert result.ok is False and result.failure is FailureReason.CLAUDE_UNAVAILABLE


def test_runner_command_is_minimal_privilege() -> None:
    command = ClaudeRunner(model="sonnet", max_turns=1).build_command("/usr/bin/claude")
    joined = " ".join(command)

    assert "-p" in command and "--output-format" in command and "json" in command
    assert "--max-turns" in command and "1" in command
    assert "--model" in command and "sonnet" in command
    assert "--restricted" in command and "--strict-mcp-config" in command
    for tool in ("Bash", "Edit", "Write"):
        assert tool in joined                       # 금지 도구로 명시
    assert "--dangerously-skip-permissions" not in joined
    assert "--continue" not in joined and "--resume" not in joined  # 개발 세션 상속 금지


# --- 2~7. 응답 해석 --------------------------------------------------------
def _wrapper(result_text: str, is_error: bool = False) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": is_error,
         "result": result_text, "duration_ms": 1200, "total_cost_usd": 0.02, "num_turns": 1}
    )


def _interpret(stdout: str, returncode: int = 0, stderr: str = ""):
    return ClaudeRunner()._interpret(returncode, stdout, stderr)


def test_valid_response_parsed() -> None:
    result = _interpret(_wrapper(json.dumps(GOOD_PAYLOAD, ensure_ascii=False)))
    assert result.ok and result.analysis.relevance_score == 0.9
    assert result.cost_usd == 0.02


def test_wrapper_noise_tolerated() -> None:
    """CLI가 경고 문구를 먼저 출력해도 wrapper를 읽는다."""
    stdout = "Warning: no stdin data received in 3s\n" + _wrapper(json.dumps(GOOD_PAYLOAD))
    assert _interpret(stdout).ok is True


def test_wrapper_success_but_invalid_payload_is_rejected() -> None:
    """CLI가 성공(is_error=false)이어도 payload가 JSON이 아니면 쓰지 않는다."""
    result = _interpret(_wrapper("I can't do this. 설명만 반환합니다."))
    assert result.ok is False and result.failure is FailureReason.INVALID_JSON


def test_invalid_schema_rejected() -> None:
    bad = dict(GOOD_PAYLOAD, relevance_score=7)
    result = _interpret(_wrapper(json.dumps(bad)))
    assert result.ok is False and result.failure is FailureReason.INVALID_SCHEMA


def test_cli_error_and_usage_limit_mapped() -> None:
    assert _interpret("", returncode=1, stderr="boom").failure is FailureReason.CLI_ERROR
    assert _interpret("", returncode=1, stderr="Usage limit reached").failure is FailureReason.USAGE_LIMIT
    assert _interpret(_wrapper("x", is_error=True)).failure is FailureReason.CLI_ERROR


def test_timeout_mapped(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")

    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(subprocess, "run", _raise)
    result = ClaudeRunner(timeout_seconds=1).run("prompt")
    assert result.ok is False and result.failure is FailureReason.TIMEOUT


def test_parse_analysis_handles_code_fence() -> None:
    text = "```json\n" + json.dumps(GOOD_PAYLOAD, ensure_ascii=False) + "\n```"
    analysis, failure, _ = parse_analysis(text)
    assert failure is None and analysis.primary_topic == "출근"


# --- 8~11. 캐시 -------------------------------------------------------------
def test_same_candidate_ten_times_calls_claude_once(config, profile, conn) -> None:
    """핵심 조건: 동일 Candidate 10회 → 실제 호출 1회, Cache Hit 9회."""
    runner = FakeRunner()
    intelligence = _intelligence(config, profile, runner, daily_request_limit=50, max_candidates_per_run=50)

    results = [intelligence.analyze(conn, MEDIA) for _ in range(10)]

    assert len(runner.calls) == 1
    assert intelligence.usage.claude_calls == 1
    assert intelligence.usage.cache_hits == 9
    assert [r.source for r in results] == ["claude"] + ["cache"] * 9
    assert get_stats(conn, today_str(9))[DAILY_METRIC] == 1


@pytest.mark.parametrize(
    "overrides,changed_media",
    [
        ({"prompt_version": "2.0"}, None),                       # Prompt Version 변경
        ({"model": "opus"}, None),                               # Model 변경
        (None, {**MEDIA, "caption": "퇴근길 지하철 #퇴근"}),       # Candidate 변경
    ],
)
def test_cache_miss_conditions(config, profile, conn, overrides, changed_media) -> None:
    runner = FakeRunner()
    first = _intelligence(config, profile, runner, daily_request_limit=50)
    first.analyze(conn, MEDIA)
    assert len(runner.calls) == 1

    second = _intelligence(config, profile, runner, daily_request_limit=50, **(overrides or {}))
    second.analyze(conn, changed_media or MEDIA)
    assert len(runner.calls) == 2   # 조건이 바뀌면 다시 호출한다


def test_cache_key_ignores_irrelevant_fields() -> None:
    base = cache_key("sonnet", "1.0", MEDIA)
    same = cache_key("sonnet", "1.0", {**MEDIA, "media_pk": 999, "discovered_at": "다름"})
    assert base == same
    assert "캡션" not in normalized_candidate_input({"caption": None})


def test_cached_value_reused_across_instances(config, profile, conn) -> None:
    runner = FakeRunner()
    _intelligence(config, profile, runner).analyze(conn, MEDIA)

    key = cache_key("sonnet", "1.0", MEDIA)
    assert get_cached(conn, key) is not None


# --- 12~13. 한도 / Pre-filter ---------------------------------------------
def test_daily_limit_blocks_call_and_falls_back(config, profile, conn) -> None:
    bump_stat(conn, today_str(9), DAILY_METRIC, 20)
    conn.commit()

    runner = FakeRunner()
    intelligence = _intelligence(config, profile, runner, daily_request_limit=20)
    result = intelligence.analyze(conn, MEDIA)

    assert runner.calls == []                       # Claude를 부르지 않는다
    assert result.ok is False and result.reason is FailureReason.USAGE_LIMIT


def test_max_candidates_per_run_enforced(config, profile, conn) -> None:
    runner = FakeRunner()
    intelligence = _intelligence(config, profile, runner, max_candidates_per_run=2, daily_request_limit=50)

    for index in range(5):
        intelligence.analyze(conn, {**MEDIA, "media_pk": index, "caption": f"캡션 {index} #출근"})

    assert len(runner.calls) == 2
    assert intelligence.usage.limited == 3


def test_prefilter_skips_low_score_candidates(config, profile, conn) -> None:
    runner = FakeRunner()
    intelligence = _intelligence(config, profile, runner, prefilter_min_score=60)

    result = intelligence.analyze(conn, MEDIA, heuristic_score=30.0)
    assert runner.calls == [] and result.reason is FailureReason.PREFILTERED

    intelligence.analyze(conn, MEDIA, heuristic_score=80.0)
    assert len(runner.calls) == 1


def test_failure_does_not_retry_same_request(config, profile, conn) -> None:
    runner = FakeRunner(results=[None])
    runner.results = [runner.failure(FailureReason.INVALID_SCHEMA, "bad")]
    intelligence = _intelligence(config, profile, runner)

    result = intelligence.analyze(conn, MEDIA)
    assert len(runner.calls) == 1                  # 즉시 재요청하지 않는다
    assert result.ok is False and result.reason is FailureReason.INVALID_SCHEMA
    assert intelligence.usage.failures == {"INVALID_SCHEMA": 1}


def test_provider_heuristic_never_calls_claude(config, profile, conn) -> None:
    runner = FakeRunner()
    intelligence = ClaudeIntelligence(config, profile, runner=runner)  # config fixture는 heuristic
    result = intelligence.analyze(conn, MEDIA)

    assert runner.calls == [] and result.reason is FailureReason.DISABLED


# --- 14~15. 댓글 필터 -------------------------------------------------------
def test_claude_comments_pass_quality_and_duplicate_filters(config, profile) -> None:
    heuristic = HeuristicAnalyzer(profile).analyze(MEDIA, ["출근", "직장인일상"])
    merged = merge_claude_analysis(heuristic, TargetingAnalysis.model_validate(GOOD_PAYLOAD))

    generator = CommentGenerator(config, profile, seed=1)
    accepted = [
        c for c in generator.generate(MEDIA, merged, CommentDuplicateFilter(config, [])) if c.quality_ok
    ]
    assert accepted and accepted[0].generator == "claude_code"
    assert all("출근" in c.text or "하늘" in c.text or "버스" in c.text for c in accepted[:1])


def test_generic_claude_comments_rejected(config, profile) -> None:
    payload = dict(GOOD_PAYLOAD, comment_candidates=["영상 잘 봤어요", "소통해요", "맞팔해요"])
    heuristic = HeuristicAnalyzer(profile).analyze(MEDIA, ["출근"])
    merged = merge_claude_analysis(heuristic, TargetingAnalysis.model_validate(payload))

    generator = CommentGenerator(config, profile, seed=1)
    results = generator.generate(MEDIA, merged, CommentDuplicateFilter(config, []))
    rejected = [c for c in results if not c.quality_ok and c.generator == "claude_code"]

    assert len(rejected) == 3                        # Generic 댓글은 전부 거절
    accepted = [c for c in results if c.quality_ok]
    assert accepted and all(c.generator == "template" for c in accepted)  # 템플릿으로 보충


def test_duplicate_claude_comment_rejected(config, profile) -> None:
    heuristic = HeuristicAnalyzer(profile).analyze(MEDIA, ["출근"])
    merged = merge_claude_analysis(heuristic, TargetingAnalysis.model_validate(GOOD_PAYLOAD))
    corpus = [GOOD_PAYLOAD["comment_candidates"][0]]

    generator = CommentGenerator(config, profile, seed=1)
    results = generator.generate(MEDIA, merged, CommentDuplicateFilter(config, corpus))
    duplicates = [c for c in results if c.quality_reason.startswith("duplicate")]

    assert duplicates and duplicates[0].text == corpus[0]


# --- Deterministic Scorer 유지 ---------------------------------------------
def test_claude_relevance_feeds_only_content_similarity(config, profile) -> None:
    heuristic = HeuristicAnalyzer(profile).analyze(MEDIA, ["출근", "직장인일상"])
    merged = merge_claude_analysis(heuristic, TargetingAnalysis.model_validate(GOOD_PAYLOAD))
    scorer = TargetScorer(config, profile)

    claude_breakdown = scorer.score(MEDIA, merged)
    heuristic_breakdown = scorer.score(MEDIA, heuristic)

    assert claude_breakdown.components["content_similarity"] == 0.9   # Claude 판단 사용
    assert "similarity:claude" in claude_breakdown.notes
    assert "similarity:heuristic" in heuristic_breakdown.notes
    # 나머지 구성요소는 Claude와 무관하게 동일하다(결정론적 계산 유지)
    for name in ("creator_fit", "activity", "engagement", "language_region", "novelty"):
        assert claude_breakdown.components[name] == heuristic_breakdown.components[name]


def test_merge_keeps_deterministic_flags(profile) -> None:
    heuristic = HeuristicAnalyzer(profile).analyze(
        {"caption": "협찬 받은 제품 #광고"}, ["광고"]
    )
    merged = merge_claude_analysis(heuristic, TargetingAnalysis.model_validate(GOOD_PAYLOAD))
    assert merged.is_ad is True        # 광고 판정은 규칙이 유지한다
    assert merged.analyzer == "claude_code"
