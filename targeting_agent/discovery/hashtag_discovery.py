"""해시태그 기반 Discovery (공식 Graph API 사용).

지원 범위(Phase 8에서 공식 문서 확인):
- `ig_hashtag_search` → `{hashtag-id}/top_media | recent_media` 읽기만 수행한다.
- recent_media는 **최근 24시간 공개 게시물**만 반환한다.
- 반환 media 객체에 **username이 포함되지 않는다.** 따라서 Creator를 식별할 수 없고,
  후보는 `unresolved:<media_id>` 임시 username으로 저장된다.
  Creator 단위 한도/cooldown을 제대로 적용하려면 운영자가 permalink를 열어
  username을 확인한 뒤 CSV Import로 보강하는 것이 안전하다.
- 7일 고유 해시태그 30개 한도는 준수 대상이다. 한도에 도달하면 조회를 중단한다.

비공식 스크래핑은 구현하지 않는다.
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

from ..core.exceptions import DiscoveryError, PlatformWarningError, RateLimitExceeded
from ..core.logger import get_logger
from ..core.models import RawCandidate

from .base import DiscoverySource, extract_hashtags
from .instagram_api import (
    HashtagQuota,
    InstagramGraphClient,
    iter_hashtags_within_quota,
)

logger = get_logger("discovery.hashtag")

UNRESOLVED_PREFIX = "unresolved:"


def media_to_candidate(
    media: dict, hashtag: str, media_type_filter: Sequence[str] = ()
) -> Optional[RawCandidate]:
    """Graph API media 객체를 RawCandidate로 변환한다.

    username을 제공받을 수 없으므로 `unresolved:<media_id>`를 임시로 사용한다.
    """
    media_id = str(media.get("id") or "").strip()
    if not media_id:
        return None

    media_type = str(media.get("media_type") or "").upper()
    # Graph API는 릴스를 VIDEO로 반환하는 경우가 있어 REEL로 정규화한다.
    normalized_type = "REEL" if media_type in ("VIDEO", "REELS", "REEL") else media_type or "REEL"
    if media_type_filter and normalized_type not in {t.upper() for t in media_type_filter}:
        return None

    caption = str(media.get("caption") or "")
    return RawCandidate(
        media_id=media_id,
        permalink=str(media.get("permalink") or f"https://www.instagram.com/p/{media_id}/"),
        username=f"{UNRESOLVED_PREFIX}{media_id}",
        caption=caption,
        hashtags=extract_hashtags(caption) or [hashtag],
        media_type=normalized_type,
        like_count=int(media.get("like_count") or 0),
        comment_count=int(media.get("comments_count") or 0),
        posted_at=str(media.get("timestamp") or "") or None,
        followers=0,  # 공식 API는 해시태그 검색 결과에 팔로워 수를 제공하지 않는다
        source=f"hashtag:{hashtag}",
        extra={"creator_unresolved": True, "hashtag": hashtag},
    )


class HashtagDiscovery(DiscoverySource):
    """해시태그 후보 수집(공식 API 읽기 전용)."""

    name = "hashtag"

    def __init__(
        self,
        hashtags: Sequence[str],
        client: Optional[InstagramGraphClient] = None,
        *,
        edge: str = "recent_media",
        per_hashtag_limit: int = 25,
        max_hashtags_per_run: int = 5,
        media_types: Sequence[str] = ("REEL",),
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.hashtags = [str(h).lstrip("#").strip() for h in hashtags if str(h).strip()]
        self.client = client or InstagramGraphClient(conn=conn)
        self.edge = edge
        self.per_hashtag_limit = int(per_hashtag_limit)
        self.max_hashtags_per_run = int(max_hashtags_per_run)
        self.media_types = tuple(media_types)

    def available(self) -> bool:
        return self.client.available()

    def discover(self, limit: Optional[int] = None) -> list[RawCandidate]:
        if not self.available():
            logger.warning(
                "Hashtag Discovery 비활성: 공식 API Credential/권한이 없습니다. "
                "CSV Import Discovery를 사용하세요."
            )
            return []

        quota = self.client.load_quota()
        allowed, skipped = iter_hashtags_within_quota(self.hashtags, quota)
        if skipped:
            logger.warning(
                "7일 해시태그 조회 한도(%d개)로 %d개를 건너뜁니다: %s",
                len(quota.used) + len(allowed),
                len(skipped),
                ", ".join(skipped[:5]),
            )
        allowed = allowed[: self.max_hashtags_per_run]

        candidates: list[RawCandidate] = []
        for hashtag in allowed:
            if limit and len(candidates) >= limit:
                break
            try:
                hashtag_id = self.client.search_hashtag_id(hashtag, quota)
                if not hashtag_id:
                    continue
                media_items = self.client.get_hashtag_media(
                    hashtag_id, edge=self.edge, limit=self.per_hashtag_limit
                )
            except RateLimitExceeded as exc:
                # 한도 초과는 우회하지 않고 즉시 중단한다.
                logger.warning("Graph API 호출 한도에 도달해 Discovery를 중단합니다: %s", exc)
                break
            except PlatformWarningError:
                self.client.save_quota(quota)
                raise
            except DiscoveryError as exc:
                logger.warning("#%s 조회 실패: %s", hashtag, exc)
                continue

            for media in media_items:
                candidate = media_to_candidate(media, hashtag, self.media_types)
                if candidate is not None:
                    candidates.append(candidate)
                if limit and len(candidates) >= limit:
                    break

        self.client.save_quota(quota)
        logger.info(
            "Hashtag Discovery: 해시태그 %d개에서 후보 %d건 수집(남은 한도 %d)",
            len(allowed),
            len(candidates),
            quota.remaining,
        )
        return candidates
