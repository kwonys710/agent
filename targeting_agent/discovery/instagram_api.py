"""Instagram Graph API 클라이언트(읽기 전용, v0.1 제한적).

공식 API 사실관계(구현 시점 확인 필요, 2024~2025 기준 Instagram Graph API):
- Hashtag Search(`ig_hashtag_search`, `/{hashtag-id}/top_media|recent_media`)는
  Instagram **Business/Creator 계정 + Facebook 앱 + 권한 심사**가 필요하다.
- Hashtag Search는 앱당 7일 윈도 기준 해시태그 조회 수 제한이 있다.
- 조회 결과는 media_id/caption/permalink/like_count 등 **공개 필드만** 제공하며,
  타인 게시물에 대한 좋아요/댓글 작성은 공식 API로 제공되지 않는다.

따라서 v0.1에서는 이 클라이언트를 '사용 가능 여부 점검 + 향후 확장 지점'으로만 둔다.
실제 호출 구현은 토큰과 권한이 준비된 뒤 Phase 8에서 공식 문서를 재확인하고 추가한다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..core.logger import get_logger

logger = get_logger("discovery.instagram_api")

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


@dataclass(frozen=True)
class GraphCredentials:
    """환경변수에서 읽은 Graph API Credential (평문 저장/로그 금지)."""

    access_token: Optional[str]
    business_account_id: Optional[str]

    @property
    def is_complete(self) -> bool:
        return bool(self.access_token and self.business_account_id)


def load_credentials() -> GraphCredentials:
    """.env/환경변수에서 Credential을 읽는다. 값 자체는 절대 로깅하지 않는다."""
    return GraphCredentials(
        access_token=os.environ.get("IG_ACCESS_TOKEN") or None,
        business_account_id=os.environ.get("IG_BUSINESS_ACCOUNT_ID") or None,
    )


class InstagramGraphClient:
    """공식 API 사용 가능 여부만 판별하는 얇은 래퍼(v0.1)."""

    def __init__(self, credentials: Optional[GraphCredentials] = None) -> None:
        self.credentials = credentials or load_credentials()

    def available(self) -> bool:
        if not self.credentials.is_complete:
            logger.info(
                "Instagram Graph API Credential이 없습니다 "
                "(IG_ACCESS_TOKEN / IG_BUSINESS_ACCOUNT_ID). 공식 API 기능은 비활성화됩니다."
            )
            return False
        return True

    def health_check(self) -> dict[str, object]:
        return {
            "credentials_present": self.credentials.is_complete,
            "base_url": GRAPH_API_BASE,
            "supported_in_v0_1": ["availability_check"],
            "not_supported": ["hashtag_search", "like_other_media", "comment_other_media"],
        }
