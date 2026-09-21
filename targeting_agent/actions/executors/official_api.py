"""OfficialAPIExecutor — 공식 Instagram Graph API 기반 Executor (v0.1 제한).

공식 API 확인 결과(Phase 8에서 재확인 필요):
- Instagram Graph API는 **타인 게시물에 대한 좋아요 작성 엔드포인트를 제공하지 않는다.**
- 댓글 작성 역시 일반적으로 **본인 소유 미디어의 댓글/답글**에 한정된다
  (`POST /{ig-comment-id}/replies`, `POST /{ig-media-id}/comments`는 본인 미디어 기준).
- 따라서 "다른 Creator의 Reel에 좋아요/댓글"은 현재 공식 API로 구현할 수 없다.

이 Executor는 그 사실을 코드로 명시하고, 잘못된 자동 실행을 막는 역할을 한다.
"""
from __future__ import annotations

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
    """공식 API로 가능한 범위만 수행한다(v0.1에서는 실행 불가로 처리)."""

    name = "official_api"
    supports_like = False
    supports_comment = False

    def __init__(self, dry_run: bool = True, client: InstagramGraphClient | None = None) -> None:
        super().__init__(dry_run=dry_run)
        self.client = client or InstagramGraphClient()

    def validate_session(self) -> bool:
        return self.client.available()

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
