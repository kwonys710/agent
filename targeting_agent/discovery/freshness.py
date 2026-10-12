"""Freshness — 최신 게시물 우선 처리 (Phase 19E).

`candidate_media.posted_at`(Phase 2부터 존재)을 그대로 쓴다.

**Target Score와 다른 개념이다.** Freshness는 "무엇을 먼저 처리할지"의
처리 우선순위이고, Target Score는 "얼마나 맞는 대상인지"의 적합도다.
오래된 콘텐츠라는 이유만으로 Target Score를 깎지 않는다
(점수의 최신성 반영은 기존 scorer.activity_score가 이미 담당한다).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from ..core.logger import get_logger

logger = get_logger("discovery.freshness")

# (최대 경과일, priority) — 작을수록 먼저 처리한다.
PRIORITY_BANDS: tuple[tuple[int, int], ...] = (
    (3, 0),    # 0~3일: 높음
    (7, 1),    # 4~7일: 중간
    (30, 2),   # 8~30일: 낮음
)
PRIORITY_OLD = 3      # 30일 초과
PRIORITY_UNKNOWN = 2  # posted_at을 모르면 중간-낮음(버리지 않는다)

BAND_LABELS = {0: "0-3일", 1: "4-7일", 2: "8-30일", 3: "30일+", PRIORITY_UNKNOWN: "8-30일"}


def _parse(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_days(posted_at: Any, *, now: Optional[datetime] = None) -> Optional[int]:
    """게시 후 경과일. 알 수 없으면 None."""
    parsed = _parse(posted_at)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now - parsed).days)


def priority(posted_at: Any, *, now: Optional[datetime] = None) -> int:
    """처리 우선순위(0이 가장 먼저)."""
    days = age_days(posted_at, now=now)
    if days is None:
        return PRIORITY_UNKNOWN
    for max_days, value in PRIORITY_BANDS:
        if days <= max_days:
            return value
    return PRIORITY_OLD


def band_label(posted_at: Any, *, now: Optional[datetime] = None) -> str:
    return BAND_LABELS.get(priority(posted_at, now=now), "알 수 없음")


@dataclass(frozen=True)
class FreshnessKey:
    """정렬 키 — (freshness priority, 신규 Creator 우선, 반복 발견 적은 순)."""

    priority: int
    creator_seen: int
    seen_count: int
    media_pk: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.priority, self.creator_seen, self.seen_count, self.media_pk)


def sort_candidates(
    rows: Sequence[Any], *, now: Optional[datetime] = None, creator_counts: Optional[dict] = None
) -> list[Any]:
    """최근 게시물과 신규 Creator를 먼저 처리하도록 정렬한다.

    점수를 바꾸지 않는다 — 처리 순서만 바꾼다.
    """
    counts = creator_counts or {}

    def key(row: Any) -> tuple[int, int, int, int]:
        data = dict(row)
        creator_id = data.get("creator_id")
        return FreshnessKey(
            priority=priority(data.get("posted_at"), now=now),
            creator_seen=int(counts.get(creator_id, 0)),
            seen_count=int(data.get("seen_count") or 1),
            media_pk=int(data.get("media_pk") or 0),
        ).as_tuple()

    return sorted(rows, key=key)


def creator_interaction_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Creator별 기존 Interaction 수(적은 Creator를 먼저 처리하기 위한 입력)."""
    return {
        int(row["creator_id"]): int(row["n"])
        for row in conn.execute(
            "SELECT creator_id, COUNT(*) AS n FROM interactions GROUP BY creator_id"
        ).fetchall()
    }


def freshness_summary(conn: sqlite3.Connection, *, now: Optional[datetime] = None) -> dict[str, int]:
    """구간별 후보 수(모니터링용)."""
    rows = conn.execute("SELECT posted_at FROM candidate_media").fetchall()
    counts: dict[str, int] = {label: 0 for label in ("0-3일", "4-7일", "8-30일", "30일+")}
    for row in rows:
        counts[band_label(row["posted_at"], now=now)] = (
            counts.get(band_label(row["posted_at"], now=now), 0) + 1
        )
    return counts
