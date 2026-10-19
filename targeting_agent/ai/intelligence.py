"""AI Intelligence 계층 (Phase 13B).

Python이 결정론적 판단을 하고, Claude Code는 콘텐츠 이해만 담당한다.

호출 전 게이트(순서대로):
  1. provider가 claude_code 인가
  2. CLI가 설치되어 있는가
  3. 캐시에 있는가            → 있으면 Claude를 부르지 않는다
  4. Pre-filter를 통과했는가   → heuristic 점수가 낮으면 부르지 않는다
  5. 이번 실행 한도(max_candidates_per_run)
  6. 오늘 한도(daily_request_limit)

어느 하나라도 막히면 heuristic fallback으로 진행한다(파이프라인은 멈추지 않는다).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..analysis.profile_analyzer import TargetProfile
from ..core.config import Config
from ..core.database import bump_stat, get_stats, today_str
from ..core.logger import get_logger
from .cache import cache_key, get_cached, save_cached
from .claude_runner import ClaudeRunner, build_prompt
from .schema import FailureReason, TargetingAnalysis

logger = get_logger("ai.intelligence")

DAILY_METRIC = "claude_requests"
CANDIDATE_FIELDS = ("username", "caption", "hashtags", "media_type", "like_count", "comment_count", "followers")


@dataclass
class IntelligenceResult:
    """한 후보에 대한 AI 처리 결과."""

    analysis: Optional[TargetingAnalysis] = None
    source: str = "fallback"  # claude | cache | fallback
    reason: Optional[FailureReason] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.analysis is not None


@dataclass
class UsageStats:
    """이번 실행의 AI 사용 집계."""

    claude_calls: int = 0
    cache_hits: int = 0
    prefiltered: int = 0
    limited: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0

    def note_failure(self, reason: Optional[FailureReason]) -> None:
        key = (reason or FailureReason.CLI_ERROR).value
        self.failures[key] = self.failures.get(key, 0) + 1


class ClaudeIntelligence:
    """Claude Code CLI 기반 콘텐츠 이해 계층."""

    def __init__(
        self,
        config: Config,
        profile: TargetProfile,
        runner: Optional[ClaudeRunner] = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.provider = str(config.get("ai.provider", "claude_code"))
        self.model = str(config.get("ai.model", "sonnet"))
        self.prompt_version = str(config.get("ai.prompt_version", "1.0"))
        self.use_cache = bool(config.get("ai.use_cache", True))
        self.fallback_to_heuristic = bool(config.get("ai.fallback_to_heuristic", True))
        self.daily_limit = int(config.get("ai.daily_request_limit", 20))
        self.max_per_run = int(config.get("ai.max_candidates_per_run", 10))
        self.prefilter_min_score = float(config.get("ai.prefilter_min_score", 60))
        self.prompt_path = self._resolve_prompt_path(config)
        self.tz_offset = int(config.get("actions.daily_limits.timezone_offset_hours", 9))
        self.runner = runner or ClaudeRunner(
            model=self.model,
            max_turns=int(config.get("ai.max_turns", 1)),
            timeout_seconds=int(config.get("ai.timeout_seconds", 120)),
        )
        self.usage = UsageStats()

    @staticmethod
    def _resolve_prompt_path(config: Config):
        """프롬프트 파일 위치. config 옆에 없으면 패키지 기본 경로를 쓴다.

        (config.yaml을 다른 폴더로 복사해 쓰는 경우에도 동작해야 한다.)
        """
        from pathlib import Path as _Path

        relative = str(config.get("ai.prompt_path", "prompts/targeting_analysis_v1.md"))
        candidate = config._resolve_path(relative)
        if candidate.exists():
            return candidate
        return _Path(__file__).resolve().parents[1] / relative

    # --- 상태 -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.provider == "claude_code"

    def available(self) -> bool:
        return self.enabled and self.runner.available()

    def used_today(self, conn: sqlite3.Connection) -> int:
        return int(get_stats(conn, today_str(self.tz_offset)).get(DAILY_METRIC, 0))

    def remaining_today(self, conn: sqlite3.Connection) -> int:
        return max(0, self.daily_limit - self.used_today(conn))

    # --- 본 처리 ----------------------------------------------------------
    def analyze(
        self,
        conn: sqlite3.Connection,
        media: Mapping[str, Any],
        *,
        heuristic_score: Optional[float] = None,
        force: bool = False,
    ) -> IntelligenceResult:
        """후보 1건을 처리한다. Claude 호출은 최대 1회, 재시도는 하지 않는다."""
        if not self.enabled:
            return IntelligenceResult(reason=FailureReason.DISABLED, detail="ai.provider != claude_code")

        key = cache_key(self.model, self.prompt_version, media)
        if self.use_cache:
            cached = get_cached(conn, key)
            if cached is not None:
                self.usage.cache_hits += 1
                return IntelligenceResult(analysis=cached, source="cache")

        if not force and heuristic_score is not None and heuristic_score < self.prefilter_min_score:
            self.usage.prefiltered += 1
            return IntelligenceResult(
                reason=FailureReason.PREFILTERED,
                detail=f"heuristic {heuristic_score:.1f} < {self.prefilter_min_score:.1f}",
            )

        if self.max_per_run and self.usage.claude_calls >= self.max_per_run:
            self.usage.limited += 1
            return IntelligenceResult(
                reason=FailureReason.USAGE_LIMIT, detail=f"max_candidates_per_run({self.max_per_run})"
            )
        if self.daily_limit and self.used_today(conn) >= self.daily_limit:
            self.usage.limited += 1
            return IntelligenceResult(
                reason=FailureReason.USAGE_LIMIT, detail=f"daily_request_limit({self.daily_limit})"
            )

        if not self.runner.available():
            return IntelligenceResult(
                reason=FailureReason.CLAUDE_UNAVAILABLE, detail="claude CLI를 찾을 수 없습니다."
            )

        try:
            prompt = build_prompt(
                self.prompt_path, self._profile_text(), self._candidate_json(media)
            )
        except OSError as exc:
            logger.warning("프롬프트 파일을 읽을 수 없습니다(%s) — heuristic으로 진행합니다.", exc)
            return IntelligenceResult(reason=FailureReason.CLI_ERROR, detail=str(exc)[:200])

        result = self.runner.run(prompt)

        # 성공·실패와 무관하게 실제 호출은 1건으로 집계한다(한도 보호).
        self.usage.claude_calls += 1
        self.usage.cost_usd += result.cost_usd
        bump_stat(conn, today_str(self.tz_offset), DAILY_METRIC, 1)
        conn.commit()

        if not result.ok or result.analysis is None:
            self.usage.note_failure(result.failure)
            logger.info(
                "Claude 분석 실패(media=%s, %s) — heuristic으로 진행합니다.",
                media.get("media_id"),
                result.failure,
            )
            return IntelligenceResult(reason=result.failure, detail=result.detail)

        if self.use_cache:
            save_cached(
                conn,
                key,
                model=self.model,
                prompt_version=self.prompt_version,
                media_pk=media.get("media_pk"),
                analysis=result.analysis,
            )
        return IntelligenceResult(analysis=result.analysis, source="claude")

    # --- 입력 구성 --------------------------------------------------------
    def _profile_text(self) -> str:
        """Target Profile을 짧은 텍스트로 요약한다(파일 전체를 넘기지 않는다)."""
        parts = [
            f"평일: {', '.join(self.profile.weekday_keywords[:10])}",
            f"주말: {', '.join(self.profile.weekend_keywords[:10])}",
            f"공통: {', '.join(self.profile.shared_keywords[:8])}",
            f"피하고 싶은 주제: {', '.join(self.profile.avoid_keywords[:8])}",
            f"언어: {self.profile.language}",
        ]
        return "\n".join(f"- {part}" for part in parts)

    @staticmethod
    def _candidate_json(media: Mapping[str, Any]) -> str:
        """후보 1건을 판단하는 데 필요한 최소 필드만 JSON으로 만든다."""
        payload: dict[str, Any] = {}
        for field_name in CANDIDATE_FIELDS:
            value = media.get(field_name)
            if field_name == "hashtags" and isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = [t.strip() for t in value.split(",") if t.strip()]
            if isinstance(value, str):
                value = " ".join(value.split())[:600]  # 긴 캡션은 잘라서 보낸다
            payload[field_name] = value
        return json.dumps(payload, ensure_ascii=False)
