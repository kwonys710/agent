"""ManualExecutor — v0.1 기본 Executor.

자동으로 Instagram에 접속하지 않는다.
운영자가 직접 처리할 수 있도록 Action 목록을 CSV/Markdown으로 내보낸다.
Dry Run 여부와 무관하게 '실행 = 지시서 생성'이며, Dry Run일 때는 파일에도 표시된다.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

from ...core.logger import get_logger
from ...core.models import ActionType, ExecutionResult, QueuedAction
from .base import BaseExecutor

logger = get_logger("actions.executor.manual")

CSV_HEADER = (
    "run_id", "action_type", "username", "permalink", "target_score", "comment_text", "dry_run", "created_at",
)


class ManualExecutor(BaseExecutor):
    """수동 처리용 Action 목록을 내보내는 Executor."""

    name = "manual"
    supports_like = True
    supports_comment = True

    def __init__(self, export_dir: Path | str, run_id: str, dry_run: bool = True) -> None:
        super().__init__(dry_run=dry_run)
        self.export_dir = Path(export_dir)
        self.run_id = run_id
        self._handle: Optional[TextIO] = None
        self._writer: Optional[csv.writer] = None  # type: ignore[assignment]
        self.export_path = self.export_dir / f"manual_actions_{run_id}.csv"

    def validate_session(self) -> bool:
        """수동 실행은 로그인 세션이 필요 없다."""
        return True

    def _ensure_writer(self) -> csv.writer:  # type: ignore[type-arg]
        if self._writer is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            # utf-8-sig: Windows Excel에서 한글이 깨지지 않도록
            self._handle = self.export_path.open("w", encoding="utf-8-sig", newline="")
            self._writer = csv.writer(self._handle)
            self._writer.writerow(CSV_HEADER)
        return self._writer

    def _write(self, action: QueuedAction) -> ExecutionResult:
        writer = self._ensure_writer()
        writer.writerow(
            [
                self.run_id,
                action.action_type.value,
                action.username,
                action.permalink,
                f"{action.target_score:.2f}",
                action.comment_text or "",
                int(self.dry_run),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ]
        )
        if self._handle:
            self._handle.flush()
        mode = "DRY-RUN" if self.dry_run else "MANUAL"
        logger.info(
            "[%s] %s @%s %s", mode, action.action_type.value, action.username, action.permalink
        )
        return self._success(f"manual_export:{self.export_path.name}")

    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        return self._write(action)

    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        if not action.comment_text:
            return self._skipped("comment_text_missing")
        return self._write(action)

    def health_check(self) -> dict[str, object]:
        info = super().health_check()
        info["export_path"] = str(self.export_path)
        return info

    def finalize(self) -> None:
        if self._handle:
            self._handle.close()
            self._handle = None
            self._writer = None
            logger.info("수동 실행 목록 저장: %s", self.export_path)
