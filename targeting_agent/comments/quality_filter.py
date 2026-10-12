"""댓글 품질 필터.

통과 조건(모두 config에서 조정 가능):
- 길이 min_length ~ max_length
- banned_phrases 미포함
- 홍보/계정유도(URL, @멘션, 해시태그, 팔로우 요청) 없음
- 이모지 개수 제한
- 목표 언어 일치
- 게시물 맥락 키워드를 최소 1개 포함(Generic Comment 차단)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..core.config import Config
from ..analysis.similarity import detect_language, normalize_text

URL_RE = re.compile(r"(https?://|www\.|\.com|\.kr\b)", re.IGNORECASE)
MENTION_RE = re.compile(r"@[0-9A-Za-z_.]+")
HASHTAG_RE = re.compile(r"#\S+")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]"
)
PROMO_PATTERNS = ("팔로우", "맞팔", "소통", "디엠", "dm", "구매", "링크", "문의", "쿠폰", "이벤트 참여")

# 문맥 없이 아무 게시물에나 붙는 표현(하드코딩 금지 원칙에 따라 config.banned_phrases와 병행)
GENERIC_PATTERNS = (
    "잘봤", "잘 봤", "잘보고", "잘 보고", "좋은영상", "좋은 영상", "구경하고", "잘 보고 갑니다",
)


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    reason: str = ""


class CommentQualityFilter:
    def __init__(self, config: Config) -> None:
        self.min_length = int(config.get("comments.min_length", 5))
        self.max_length = int(config.get("comments.max_length", 60))
        self.language = str(config.get("comments.language", "ko"))
        self.allow_emoji = bool(config.get("comments.allow_emoji", True))
        self.max_emoji = int(config.get("comments.max_emoji", 1))
        self.banned = [str(p) for p in config.list_of("comments.banned_phrases")]
        self.avoid_generic = bool(config.get("comments.avoid_generic_comments", True))
        self.require_context = bool(config.get("comments.require_context_keyword", True))

    def check(self, text: str, context_keywords: Sequence[str] = ()) -> QualityResult:
        """댓글 한 건의 품질을 판정한다."""
        normalized = normalize_text(text)
        if not normalized:
            return QualityResult(False, "empty")

        length = len(normalized)
        if length < self.min_length:
            return QualityResult(False, f"too_short({length})")
        if length > self.max_length:
            return QualityResult(False, f"too_long({length})")

        lowered = normalized.lower()
        for phrase in self.banned:
            if phrase and phrase.lower() in lowered:
                return QualityResult(False, f"banned_phrase:{phrase}")
        if self.avoid_generic:
            for pattern in GENERIC_PATTERNS:
                if pattern in lowered:
                    return QualityResult(False, f"generic:{pattern}")
        for pattern in PROMO_PATTERNS:
            if pattern in lowered:
                return QualityResult(False, f"promotional:{pattern}")

        if URL_RE.search(normalized):
            return QualityResult(False, "contains_url")
        if MENTION_RE.search(normalized):
            return QualityResult(False, "contains_mention")
        if HASHTAG_RE.search(normalized):
            return QualityResult(False, "contains_hashtag")

        emojis = EMOJI_RE.findall(normalized)
        if emojis and not self.allow_emoji:
            return QualityResult(False, "emoji_not_allowed")
        if len(emojis) > self.max_emoji:
            return QualityResult(False, f"too_many_emoji({len(emojis)})")

        if self.language == "ko" and detect_language(normalized) not in ("ko", "mixed"):
            return QualityResult(False, "language_mismatch")

        if self.require_context and context_keywords:
            if not any(
                str(keyword).lower() in lowered
                for keyword in context_keywords
                if len(str(keyword)) >= 2
            ):
                return QualityResult(False, "no_context_keyword")

        return QualityResult(True, "")
