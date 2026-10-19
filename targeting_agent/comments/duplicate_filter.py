"""댓글 중복 필터.

기존에 생성했거나 실제로 사용한 댓글과의 의미 유사도가
config.comments.duplicate_similarity_threshold(기본 0.86) 이상이면 폐기한다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..analysis.similarity import max_similarity, normalize_text
from ..core.config import Config
from ..core.database import recent_comment_texts


@dataclass(frozen=True)
class DuplicateResult:
    is_duplicate: bool
    similarity: float
    matched_text: str = ""


class CommentDuplicateFilter:
    """DB에 쌓인 댓글 코퍼스를 한 번만 읽어 재사용한다(반복 조회 방지)."""

    def __init__(self, config: Config, corpus: Optional[Sequence[str]] = None) -> None:
        self.threshold = float(config.get("comments.duplicate_similarity_threshold", 0.86))
        self.limit = int(config.get("comments.duplicate_compare_limit", 300))
        self._corpus: list[str] = list(corpus or [])

    @classmethod
    def from_db(cls, config: Config, conn: sqlite3.Connection) -> "CommentDuplicateFilter":
        limit = int(config.get("comments.duplicate_compare_limit", 300))
        return cls(config, recent_comment_texts(conn, limit=limit))

    @property
    def corpus_size(self) -> int:
        return len(self._corpus)

    def check(self, text: str) -> DuplicateResult:
        score, matched = max_similarity(text, self._corpus)
        return DuplicateResult(score >= self.threshold, round(score, 4), matched)

    def remember(self, text: str) -> None:
        """이번 실행에서 새로 만든 댓글도 즉시 비교 대상에 넣는다."""
        normalized = normalize_text(text)
        if normalized:
            self._corpus.append(normalized)

    def extend(self, texts: Iterable[str]) -> None:
        for text in texts:
            self.remember(text)
