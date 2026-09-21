"""Discovery 공통 인터페이스.

후보 수집 방식(CSV Import / Hashtag / 공식 API)이 달라져도
파이프라인은 DiscoverySource 인터페이스만 사용한다.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Iterable, Optional, Sequence

from ..core.models import RawCandidate

HASHTAG_RE = re.compile(r"#([0-9A-Za-z_가-힣]+)")
SHORTCODE_RE = re.compile(r"instagram\.com/(?:reel|reels|p|tv)/([0-9A-Za-z_-]+)")


class DiscoverySource(ABC):
    """후보(candidate) 수집기."""

    name: str = "base"

    @abstractmethod
    def discover(self, limit: Optional[int] = None) -> list[RawCandidate]:
        """후보 목록을 반환한다. 저장/중복 제거는 호출자가 담당한다."""

    def available(self) -> bool:
        """이 소스를 현재 환경에서 사용할 수 있는지 여부."""
        return True


def extract_hashtags(text: str) -> list[str]:
    """캡션에서 해시태그를 추출한다(# 제외, 중복 제거, 순서 유지)."""
    seen: list[str] = []
    for tag in HASHTAG_RE.findall(text or ""):
        lowered = tag.lower()
        if lowered not in seen:
            seen.append(lowered)
    return seen


def shortcode_from_permalink(permalink: str) -> Optional[str]:
    """permalink에서 Instagram shortcode를 추출한다."""
    match = SHORTCODE_RE.search(permalink or "")
    return match.group(1) if match else None


def dedupe_candidates(candidates: Iterable[RawCandidate]) -> tuple[list[RawCandidate], int]:
    """같은 실행 안에서의 media_id 중복을 제거한다. (결과, 제거된 수)"""
    seen: set[str] = set()
    unique: list[RawCandidate] = []
    duplicates = 0
    for candidate in candidates:
        if candidate.media_id in seen:
            duplicates += 1
            continue
        seen.add(candidate.media_id)
        unique.append(candidate)
    return unique, duplicates


def filter_by_config(
    candidates: Sequence[RawCandidate],
    *,
    languages: Sequence[str],
    content_types: Sequence[str],
    min_followers: int,
    max_followers: int,
    skip_private: bool,
    skip_ads: bool,
) -> tuple[list[RawCandidate], dict[str, int]]:
    """config 기준으로 명백히 대상이 아닌 후보를 걸러낸다."""
    kept: list[RawCandidate] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    allowed_types = {t.upper() for t in content_types}
    for candidate in candidates:
        if allowed_types and candidate.media_type.upper() not in allowed_types:
            reject("media_type")
            continue
        if skip_private and candidate.is_private:
            reject("private")
            continue
        if skip_ads and candidate.is_ad:
            reject("ad")
            continue
        followers = int(candidate.followers or 0)
        if followers and not (min_followers <= followers <= max_followers):
            reject("followers")
            continue
        if languages and candidate.language and candidate.language not in languages:
            reject("language")
            continue
        kept.append(candidate)
    return kept, rejected
