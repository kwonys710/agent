"""콘텐츠 분석기.

provider:
- "heuristic": 캡션/해시태그 기반 규칙 분석. 외부 API 비용 0, 오프라인 동작.
- "gemini"   : Gemini API 사용. API Key가 없거나 호출 실패 시
               analysis.fallback_to_heuristic 설정에 따라 heuristic으로 대체.

비용 절감 원칙:
- 동일 media + 동일 analyzer/analyzer_version/prompt_version 조합은 DB 캐시를 재사용한다.
- 캐시 무효화는 버전 값을 올려서 한다(config.analysis.analyzer_version/prompt_version).
"""
from __future__ import annotations

import json
import os
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


class GeminiAnalyzer:
    """Gemini API 기반 분석기.

    google-genai 패키지와 GEMINI_API_KEY가 모두 있어야 동작한다.
    둘 중 하나라도 없으면 available()이 False이고, 호출자가 fallback을 결정한다.
    """

    name = "gemini"

    PROMPT = (
        "너는 인스타그램 릴스 콘텐츠 분석기다. 아래 게시물의 캡션과 해시태그를 보고 "
        "JSON만 출력해라. 키: topics(문자열 배열, 최대 4개), keywords(문자열 배열, 최대 10개), "
        "language(ko/en/mixed), tone(짧은 한국어 단어), summary(한국어 한 문장, 40자 이내), "
        "is_ad(boolean), is_sensitive(boolean).\n\n캡션:\n{caption}\n\n해시태그: {hashtags}"
    )

    def __init__(self, model: str, version: str = "1", prompt_version: str = "1") -> None:
        self.model = model
        self.version = version
        self.prompt_version = prompt_version
        self._client: Optional[Any] = None

    def available(self) -> bool:
        if not os.environ.get("GEMINI_API_KEY"):
            logger.info("GEMINI_API_KEY가 없어 Gemini 분석을 사용할 수 없습니다.")
            return False
        try:  # pragma: no cover - 패키지 설치 환경에서만 실행
            from google import genai  # type: ignore # noqa: F401
        except ImportError:
            logger.info("google-genai 패키지가 없어 Gemini 분석을 사용할 수 없습니다.")
            return False
        return True

    def _get_client(self) -> Any:  # pragma: no cover - 실제 API 호출 경로
        if self._client is None:
            from google import genai  # type: ignore

            self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        return self._client

    def analyze(self, media: Mapping[str, Any], hashtags: Sequence[str]) -> ContentAnalysis:  # pragma: no cover
        prompt = self.PROMPT.format(
            caption=str(media.get("caption") or ""), hashtags=", ".join(hashtags)
        )
        response = self._get_client().models.generate_content(model=self.model, contents=prompt)
        text = (getattr(response, "text", "") or "").strip()
        payload = _extract_json(text)
        analysis = ContentAnalysis.from_mapping(payload)
        analysis.analyzer = self.name
        analysis.analyzer_version = self.version
        analysis.prompt_version = self.prompt_version
        return analysis


def _extract_json(text: str) -> Mapping[str, Any]:  # pragma: no cover - API 응답 경로
    """```json 블록 등 잡음이 섞인 응답에서 JSON 객체만 추출한다."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}


class ContentAnalyzer:
    """provider 선택 + 캐시 재사용을 담당하는 Facade."""

    def __init__(self, config: Config, profile: TargetProfile) -> None:
        self.config = config
        self.profile = profile
        self.analyzer_version = str(config.get("analysis.analyzer_version", "1"))
        self.prompt_version = str(config.get("analysis.prompt_version", "1"))
        self.heuristic = HeuristicAnalyzer(profile, version=f"{self.analyzer_version}-p{profile.version}")
        self.provider = str(config.get("analysis.provider", "heuristic"))
        self.fallback = bool(config.get("analysis.fallback_to_heuristic", True))
        self._gemini: Optional[GeminiAnalyzer] = None
        self._active = self._resolve_active()

    def _resolve_active(self) -> Any:
        if self.provider == "gemini":
            gemini = GeminiAnalyzer(
                model=str(self.config.get("analysis.model", "gemini-2.0-flash")),
                version=self.analyzer_version,
                prompt_version=self.prompt_version,
            )
            if gemini.available():
                self._gemini = gemini
                return gemini
            if not self.fallback:
                from ..core.exceptions import AnalysisError

                raise AnalysisError(
                    "analysis.provider=gemini 인데 GEMINI_API_KEY 또는 google-genai가 없습니다. "
                    "analysis.fallback_to_heuristic: true 로 두거나 Key를 설정하세요."
                )
            logger.warning("Gemini를 사용할 수 없어 heuristic 분석기로 대체합니다.")
        return self.heuristic

    @property
    def active_name(self) -> str:
        return getattr(self._active, "name", "heuristic")

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
        try:
            analysis = analyzer.analyze(media_row, hashtags)
        except Exception as exc:  # pragma: no cover - 외부 API 실패 경로
            if analyzer is self.heuristic or not self.fallback:
                raise
            logger.warning("Gemini 분석 실패(%s) — heuristic으로 대체합니다.", exc)
            analysis = self.heuristic.analyze(media_row, hashtags)

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
