"""DailyReels Target Profile 로딩 및 Creator 적합도 판단.

Profile은 코드에 고정하지 않는다.
1) profiles/*.yaml 에서 읽고
2) DB app_state['target_profile'](JSON)에 override가 있으면 그것을 우선한다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from ..core.exceptions import ConfigError
from ..core.logger import get_logger

logger = get_logger("analysis.profile")


@dataclass(frozen=True)
class TargetProfile:
    """내 콘텐츠(DailyReels) 성격 정의."""

    version: str = "1"
    language: str = "ko"
    weekday_keywords: tuple[str, ...] = ()
    weekend_keywords: tuple[str, ...] = ()
    shared_keywords: tuple[str, ...] = ()
    avoid_keywords: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    preferred_media_types: tuple[str, ...] = ("REEL",)
    source: str = "file"
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def all_keywords(self) -> tuple[str, ...]:
        seen: list[str] = []
        for group in (self.weekday_keywords, self.weekend_keywords, self.shared_keywords):
            for keyword in group:
                if keyword not in seen:
                    seen.append(keyword)
        return tuple(seen)

    def is_avoided(self, tokens: Sequence[str]) -> Optional[str]:
        """회피 키워드에 걸리면 해당 키워드를 반환한다."""
        lowered = {t.lower() for t in tokens}
        for keyword in self.avoid_keywords:
            key = keyword.lower()
            if key in lowered or any(key in token for token in lowered):
                return keyword
        return None


def _tuple(data: Mapping[str, Any], *path: str) -> tuple[str, ...]:
    node: Any = data
    for part in path:
        if not isinstance(node, Mapping):
            return ()
        node = node.get(part)
    if not node:
        return ()
    if isinstance(node, str):
        return (node,)
    return tuple(str(v).strip() for v in node if str(v).strip())


def profile_from_mapping(data: Mapping[str, Any], source: str = "file") -> TargetProfile:
    topics = _tuple(data, "topics_weekday") + _tuple(data, "topics_weekend") + _tuple(data, "topics")
    return TargetProfile(
        version=str(data.get("version", "1")),
        language=str(data.get("language", "ko")),
        weekday_keywords=_tuple(data, "weekday", "keywords"),
        weekend_keywords=_tuple(data, "weekend", "keywords"),
        shared_keywords=_tuple(data, "shared", "keywords"),
        avoid_keywords=_tuple(data, "avoid_keywords"),
        topics=topics,
        preferred_media_types=_tuple(data, "preferred_media_types") or ("REEL",),
        source=source,
        extra=dict(data),
    )


def load_profile(
    path: Path | str,
    conn: Optional[sqlite3.Connection] = None,
) -> TargetProfile:
    """Profile을 로드한다. DB override가 있으면 우선 적용한다."""
    if conn is not None:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = 'target_profile'"
        ).fetchone()
        if row and row["value"]:
            try:
                return profile_from_mapping(json.loads(row["value"]), source="db")
            except json.JSONDecodeError:
                logger.warning("app_state.target_profile JSON 파싱 실패 — 파일 Profile을 사용합니다.")

    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Target Profile 파일을 찾을 수 없습니다: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("Target Profile 최상위는 매핑이어야 합니다.")
    return profile_from_mapping(data, source=str(path))


def creator_fit(
    followers: int,
    *,
    min_followers: int,
    max_followers: int,
    is_private: bool,
) -> float:
    """Creator 규모 적합도(0~1).

    목표 구간 안이면 1.0에 가깝고, 구간을 벗어날수록 선형으로 감소한다.
    비공개 계정은 Interaction 대상이 아니므로 0."""
    if is_private:
        return 0.0
    if followers <= 0:
        return 0.5  # 정보 없음 — 중립값
    if min_followers <= followers <= max_followers:
        # 구간 중앙(로그 스케일)에 가까울수록 소폭 가산
        span = max(max_followers - min_followers, 1)
        center = min_followers + span / 2
        distance = abs(followers - center) / span
        return max(0.75, 1.0 - distance * 0.5)
    if followers < min_followers:
        return max(0.0, followers / max(min_followers, 1) * 0.6)
    overflow = (followers - max_followers) / max(max_followers, 1)
    return max(0.0, 0.6 - min(overflow, 1.0) * 0.6)
