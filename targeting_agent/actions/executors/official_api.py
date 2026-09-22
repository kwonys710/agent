"""OfficialAPIExecutor — 공식 Instagram Graph API 기반 Executor.

Phase 8 공식 문서 확인 결과:
- 공식 API는 **타인 게시물에 대한 좋아요/댓글 작성을 제공하지 않는다.**
  공개 댓글 작성 엔드포인트는 제거되었고, 이후 추가된 engagement 관련 기능도
  본인 소유 콘텐츠(내 게시물/내 게시물의 댓글) 기준으로 안내된다.
- 따라서 "다른 Creator의 Reel에 좋아요/댓글"이라는 이 프로젝트의 목적은
  공식 API로 구현할 수 없다. 이 Executor는 그 사실을 코드로 고정하고,
  잘못된 자동 실행을 막는 역할을 한다.

공식 API로 실제 수행하는 일:
- validate_session(): Access Token이 유효한지 IG User 노드 조회로 확인
- health_check(): 사용 가능한 읽기 기능과 해시태그 조회 한도 사용량 보고
"""
from __future__ import annotations

from typing import Optional

from ...core.logger import get_logger
from ...core.models import ExecutionResult, QueuedAction
from ...discovery.instagram_api import InstagramGraphClient
from .base import BaseExecutor

logger = get_logger("actions.executor.official_api")

UNSUPPORTED = (
    "공식 Instagram Graph API는 타인 게시물에 대한 좋아요/댓글 작성을 지원하지 않습니다. "
    "executor.mode를 manual로 두고 운영자가 직접 처리하세요."
)


class OfficialAPIExecutor(BaseExecutor):
    """공식 API로 가능한 범위만 수행한다(쓰기 동작 없음)."""

    name = "official_api"
    supports_like = False
    supports_comment = False

    def __init__(
        self, dry_run: bool = True, client: Optional[InstagramGraphClient] = None
    ) -> None:
        super().__init__(dry_run=dry_run)
        self.client = client or InstagramGraphClient()

    def validate_session(self) -> bool:
        """Credential 존재 여부 + 토큰 실제 동작 여부를 확인한다."""
        if not self.client.available():
            return False
        if not self.client.validate_token():
            logger.warning("Graph API Access Token이 유효하지 않습니다(재인증 필요).")
            return False
        logger.warning(
            "공식 API 토큰은 유효하지만 쓰기 동작은 지원되지 않습니다. %s", UNSUPPORTED
        )
        return False  # 쓰기가 불가능하므로 Action 실행 자체를 시작하지 않는다

    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        logger.warning("LIKE 미지원: %s", UNSUPPORTED)
        return self._skipped("official_api_unsupported_like")

    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        logger.warning("COMMENT 미지원: %s", UNSUPPORTED)
        return self._skipped("official_api_unsupported_comment")

    def health_check(self) -> dict[str, object]:
        info = super().health_check()
        info.update(self.client.health_check())
        info["note"] = UNSUPPORTED
        return info
