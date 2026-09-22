"""콘텐츠 분석기.

분석 계층은 두 단계다.
1. heuristic  : 캡션/해시태그 기반 규칙 분석. 비용 0, 항상 먼저 수행한다.
2. Claude Code: `ai.provider: claude_code`일 때 의미 판단을 덧입힌다(Phase 13).
   Claude를 쓸 수 없거나 한도에 걸리면 1번 결과로 그대로 진행한다.

LLM API SDK(OpenAI/Gemini/Anthropic)는 사용하지 않는다.
비용 절감 원칙: 동일 media + 동일 analyzer/version 조합은 DB 캐시를 재사용한다.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Optional, Sequence

from ..core.config import Config
from ..core.database import get_cached_analysis, save_analysis
from ..core.logger import get_logger
from ..core.models import ContentAnalysis
from .profile_analyzer import TargetProfile
from .similarity import detect_language, tokenize

logger = get_logger("analysis.content")

SENSITIVE_KEYWORDS = (
    "도박", "성인", "19금", "대출", "코인리딩", "주식리딩", "카지노", "베팅",
    "정치", "혐오", "사고", "부고",
)
AD_KEYWORDS = ("광고", "협찬", "유료광고", "제휴", "공구", "sponsored", "ad", "paid")

TONE_HINTS = {
    "지침": "tired",
    "피곤": "tired",
    "힘들": "tired",
    "행복": "positive",
    "좋아": "positive",
    "최고": "positive",
    "힐링": "calm",
    "여유": "calm",
    "바쁘": "busy",
}


class HeuristicAnalyzer:
    """캡션/해시태그 기반 규칙 분석기(기본값, 비용 0)."""

    name = "heuristic"

    def __init__(self, profile: TargetProfile, version: str = "1") -> None:
        self.profile = profile
        self.version = version

    def analyze(self, media: Mapping[str, Any], hashtags: Sequence[str]) -> ContentAnalysis:
        caption = str(media.get("caption") or "")
        text = f"{caption} {' '.join(hashtags)}".strip()
        lowered = text.lower()

        keywords = self._keywords(caption, hashtags)
        topics = self._topics(keywords, lowered)
        return ContentAnalysis(
            topics=topics,
            keywords=keywords,
            language=str(media.get("language") or "") or detect_language(text),
            tone=self._tone(lowered),
            summary=self._summary(caption, topics),
            is_ad=bool(media.get("is_ad")) or any(k in lowered for k in AD_KEYWORDS),
            is_sensitive=any(k in lowered for k in SENSITIVE_KEYWORDS),
            analyzer=self.name,
            analyzer_version=self.version,
            prompt_version="0",
        )

    def _keywords(self, caption: str, hashtags: Sequence[str]) -> list[str]:
        keywords: list[str] = []
        for tag in hashtags:
            tag = str(tag).lstrip("#").lower()
            if tag and tag not in keywords:
                keywords.append(tag)
        for token in tokenize(caption):
            if token not in keywords:
                keywords.append(token)
        return keywords[:25]

    def _topics(self, keywords: Sequence[str], lowered: str) -> list[str]:
        topics: list[str] = []
        groups = (
            ("직장생활", self.profile.weekday_keywords),
            ("주말일상", self.profile.weekend_keywords),
            ("일상브이로그", self.profile.shared_keywords),
        )
        for topic, group in groups:
            for keyword in group:
                key = keyword.lower()
                if key in lowered or any(key in k for k in keywords):
                    topics.append(topic)
                    break
        return topics or ["기타"]

    def _tone(self, lowered: str) -> str:
        for hint, tone in TONE_HINTS.items():
            if hint in lowered:
                return tone
        return "neutral"

    def _summary(self, caption: str, topics: Sequence[str]) -> str:
        head = " ".join(caption.split())[:60]
        return f"[{'/'.join(topics)}] {head}".strip()


def merge_claude_analysis(base: ContentAnalysis, claude: Any) -> ContentAnalysis:
    """heuristic 분석에 Claude의 콘텐츠 이해 결과를 덧입힌다.

    Claude는 의미 판단만 담당한다. 광고/민감 판정 같은 결정론적 플래그는
    heuristic 결과를 유지한다(규칙을 모델 응답으로 덮어쓰지 않기 위함).
    """
    topics = [t for t in (claude.topics or []) if t] or base.topics
    if claude.primary_topic and claude.primary_topic not in topics:
        topics = [claude.primary_topic, *topics]
    return ContentAnalysis(
        topics=topics[:6],
        keywords=base.keywords,
        language=claude.language if claude.language != "unknown" else base.language,
        tone=claude.mood or base.tone,
        summary=claude.summary or base.summary,
        is_ad=base.is_ad,
        is_sensitive=base.is_sensitive,
        analyzer="claude_code",
        analyzer_version=base.analyzer_version,
        prompt_version=base.prompt_version,
        relevance_score=claude.relevance_score,
        relevance_reason=claude.relevance_reason,
        comment_candidates=list(claude.comment_candidates or []),
    )


class ContentAnalyzer:
    """provider 선택 + 캐시 재사용을 담당하는 Facade."""

    def __init__(self, config: Config, profile: TargetProfile) -> None:
        self.config = config
        self.profile = profile
        self.analyzer_version = str(config.get("analysis.analyzer_version", "1"))
        self.prompt_version = str(config.get("analysis.prompt_version", "1"))
        self.heuristic = HeuristicAnalyzer(
            profile, version=f"{self.analyzer_version}-p{profile.version}"
        )
        self._active = self.heuristic

    @property
    def active_name(self) -> str:
        return self.heuristic.name

    def analyze_media(
        self, conn: sqlite3.Connection, media_row: Mapping[str, Any]
    ) -> tuple[ContentAnalysis, bool]:
        """분석 결과와 캐시 사용 여부를 반환한다."""
        media_pk = int(media_row["media_pk"])
        analyzer = self._active
        analyzer_version = getattr(analyzer, "version", self.analyzer_version)
        prompt_version = getattr(analyzer, "prompt_version", "0")

        cached = get_cached_analysis(
            conn, media_pk, analyzer.name, str(analyzer_version), str(prompt_version)
        )
        if cached is not None:
            return ContentAnalysis.from_mapping(json.loads(cached["payload"])), True

        hashtags = _load_hashtags(media_row)
        analysis = analyzer.analyze(media_row, hashtags)
        save_analysis(conn, media_pk, analysis)
        return analysis, False


def _load_hashtags(media_row: Mapping[str, Any]) -> list[str]:
    raw = media_row.get("hashtags")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw]
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return [t.strip() for t in str(raw).split(",") if t.strip()]
    return [str(t) for t in value] if isinstance(value, list) else []
