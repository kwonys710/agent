"""ManualExecutor — v0.1 기본 Executor.

자동으로 Instagram에 접속하지 않는다.
운영자가 직접 처리할 수 있도록 Action 목록을 CSV + 체크리스트(Markdown)로 내보낸다.

실행 결과 표시(Phase 9):
- Dry Run  : SUCCESS — 실제 처리가 없는 시뮬레이션이므로 그대로 기록한다.
- 실제 모드 : APPROVED — 목록에 올렸을 뿐 아직 처리되지 않았다.
             운영자가 Instagram에서 직접 처리한 뒤 `--confirm`으로 확인해야
             Interaction으로 기록된다(하지도 않은 일을 했다고 기록하지 않기 위함).
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

from ...core.logger import get_logger
from ...core.models import ActionStatus, ActionType, ExecutionResult, QueuedAction
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
        self._checklist: list[str] = []
        self.export_path = self.export_dir / f"manual_actions_{run_id}.csv"
        self.checklist_path = self.export_dir / f"manual_actions_{run_id}.md"

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
        self._checklist.append(self._checklist_line(action))

        mode = "DRY-RUN" if self.dry_run else "MANUAL"
        logger.info(
            "[%s] %s @%s %s", mode, action.action_type.value, action.username, action.permalink
        )
        if self.dry_run:
            return self._success(f"manual_export:{self.export_path.name}")
        # 실제 모드: 아직 처리되지 않았으므로 '확인 대기'로 남긴다.
        return ExecutionResult(
            success=False,
            status=ActionStatus.APPROVED,
            detail=f"awaiting_manual_confirm:{self.export_path.name}",
            dry_run=False,
        )

    @staticmethod
    def _checklist_line(action: QueuedAction) -> str:
        """운영자가 그대로 보고 처리할 수 있는 체크리스트 한 줄."""
        head = (
            f"- [ ] **{action.action_type.value}** @{action.username} "
            f"(점수 {action.target_score:.1f}, Action ID `{action.action_id}`)\n"
            f"  - {action.permalink}"
        )
        if action.comment_text:
            head += f"\n  - 댓글: `{action.comment_text}`"
        return head

    def execute_like(self, action: QueuedAction) -> ExecutionResult:
        return self._write(action)

    def execute_comment(self, action: QueuedAction) -> ExecutionResult:
        if not action.comment_text:
            return self._skipped("comment_text_missing")
        return self._write(action)

    def health_check(self) -> dict[str, object]:
        info = super().health_check()
        info["export_path"] = str(self.export_path)
        info["checklist_path"] = str(self.checklist_path)
        return info

    def _write_checklist(self) -> None:
        if not self._checklist:
            return
        mode = "Dry Run(실제 처리 없음)" if self.dry_run else "실제 처리 대상"
        body = [
            f"# 수동 처리 목록 — {self.run_id}",
            "",
            f"- 모드: {mode}",
            f"- 건수: {len(self._checklist)}",
        ]
        if not self.dry_run:
            body += [
                "",
                "처리한 뒤 확인 명령을 실행하세요:",
                "",
                "```bat",
                "run_targeting.bat --confirm all      :: 전부 처리 완료로 기록",
                "run_targeting.bat --confirm 12,13    :: 일부만 기록",
                "run_targeting.bat --skip 14          :: 처리하지 않은 건 취소",
                "```",
            ]
        body += ["", *self._checklist, ""]
        self.checklist_path.write_text("\n".join(body), encoding="utf-8")

    def finalize(self) -> None:
        if self._handle:
            self._handle.close()
            self._handle = None
            self._writer = None
            logger.info("수동 실행 목록 저장: %s", self.export_path)
        self._write_checklist()
