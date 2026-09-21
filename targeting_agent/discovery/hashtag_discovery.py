"""해시태그 기반 Discovery (v0.1 Stub).

공식 Graph API의 Hashtag Search 권한/토큰이 준비되기 전에는 후보를 반환하지 않는다.
비공식 스크래핑은 구현하지 않는다(플랫폼 정책 위반 및 탐지 우회에 해당).
"""
from __future__ import annotations

from typing import Optional, Sequence

from ..core.logger import get_logger
from ..core.models import RawCandidate

from .base import DiscoverySource
from .instagram_api import InstagramGraphClient

logger = get_logger("discovery.hashtag")


class HashtagDiscovery(DiscoverySource):
    """해시태그 후보 수집. 공식 API가 준비되면 Phase 8에서 실제 호출을 연결한다."""

    name = "hashtag"

    def __init__(
        self,
        hashtags: Sequence[str],
        client: Optional[InstagramGraphClient] = None,
    ) -> None:
        self.hashtags = list(hashtags)
        self.client = client or InstagramGraphClient()

    def available(self) -> bool:
        return self.client.available()

    def discover(self, limit: Optional[int] = None) -> list[RawCandidate]:
        if not self.available():
            logger.warning(
                "Hashtag Discovery 비활성: 공식 API Credential/권한이 없습니다. "
                "CSV Import Discovery를 사용하세요."
            )
            return []
        logger.warning(
            "Hashtag Discovery는 v0.1에서 Stub입니다 "
            "(대상 해시태그 %d개). Phase 8에서 공식 API 지원 범위 확인 후 구현합니다.",
            len(self.hashtags),
        )
        return []
