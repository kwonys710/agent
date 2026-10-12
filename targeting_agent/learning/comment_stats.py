"""댓글 선호 통계 (Phase 17).

NLP 모델을 새로 만들지 않는다. 저장된 댓글 Draft와 Feedback만으로
결정론적 지표를 집계해 향후 Prompt 개선 참고 자료로 남긴다.
Claude를 호출하지 않는다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..analysis.similarity import text_similarity
from ..comments.quality_filter import EMOJI_RE

OPERATOR = "operator"


@dataclass
class EditComparison:
    """운영자가 고친 댓글 1건과 원본의 차이."""

    original: str
    edited: str
    length_delta: int
    emoji_delta: int
    question_added: bool
    exclamation_added: bool
    similarity: float


@dataclass
class CommentPreference:
    """댓글 선호 요약."""

    selected_count: int = 0
    avg_length: float = 0.0
    emoji_rate: float = 0.0
    edited_count: int = 0
    edit_rate: float = 0.0
    avg_length_delta: float = 0.0
    emoji_added: int = 0
    good_comment: int = 0
    bad_comment: int = 0
    edits: list[EditComparison] = field(default_factory=list)

    def as_rows(self) -> dict[str, str]:
        return {
            "선택된 댓글": str(self.selected_count),
            "평균 길이": f"{self.avg_length:.1f}자",
            "이모지 사용률": f"{self.emoji_rate * 100:.0f}%",
            "직접 수정": f"{self.edited_count} ({self.edit_rate * 100:.0f}%)",
            "수정 시 길이 변화": f"{self.avg_length_delta:+.1f}자",
            "이모지 추가": str(self.emoji_added),
            "GOOD_COMMENT / BAD_COMMENT": f"{self.good_comment} / {self.bad_comment}",
        }


def _emoji_count(text: str) -> int:
    return len(EMOJI_RE.findall(text or ""))


def _best_original(conn: sqlite3.Connection, media_pk: int, edited: str) -> Optional[str]:
    """수정본과 가장 비슷한 생성본을 원본으로 본다."""
    rows = conn.execute(
        "SELECT text FROM comment_drafts WHERE media_pk = ? AND generator != ?",
        (media_pk, OPERATOR),
    ).fetchall()
    best_text, best_score = None, 0.0
    for row in rows:
        score = text_similarity(edited, str(row["text"]))
        if score > best_score:
            best_text, best_score = str(row["text"]), score
    return best_text


def collect_comment_preference(conn: sqlite3.Connection) -> CommentPreference:
    """선택/수정된 댓글에서 선호 지표를 집계한다."""
    selected = conn.execute(
        "SELECT media_pk, text, generator FROM comment_drafts WHERE status = 'SELECTED'"
    ).fetchall()
    preference = CommentPreference(selected_count=len(selected))

    if selected:
        lengths = [len(str(row["text"])) for row in selected]
        preference.avg_length = sum(lengths) / len(lengths)
        preference.emoji_rate = sum(
            1 for row in selected if _emoji_count(str(row["text"]))
        ) / len(selected)

    edited_rows = [row for row in selected if str(row["generator"]) == OPERATOR]
    preference.edited_count = len(edited_rows)
    preference.edit_rate = len(edited_rows) / len(selected) if selected else 0.0

    deltas: list[int] = []
    for row in edited_rows:
        edited_text = str(row["text"])
        original = _best_original(conn, int(row["media_pk"]), edited_text)
        if original is None:
            continue
        comparison = EditComparison(
            original=original,
            edited=edited_text,
            length_delta=len(edited_text) - len(original),
            emoji_delta=_emoji_count(edited_text) - _emoji_count(original),
            question_added="?" in edited_text and "?" not in original,
            exclamation_added="!" in edited_text and "!" not in original,
            similarity=round(text_similarity(original, edited_text), 3),
        )
        preference.edits.append(comparison)
        deltas.append(comparison.length_delta)
        if comparison.emoji_delta > 0:
            preference.emoji_added += 1

    if deltas:
        preference.avg_length_delta = sum(deltas) / len(deltas)

    counts = {
        row["feedback_type"]: int(row["n"])
        for row in conn.execute(
            "SELECT feedback_type, COUNT(*) AS n FROM feedback "
            "WHERE feedback_type IN ('GOOD_COMMENT', 'BAD_COMMENT') GROUP BY feedback_type"
        )
    }
    preference.good_comment = counts.get("GOOD_COMMENT", 0)
    preference.bad_comment = counts.get("BAD_COMMENT", 0)
    return preference
